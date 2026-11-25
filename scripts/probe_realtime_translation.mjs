import fs from "node:fs/promises";
import path from "node:path";
import { performance } from "node:perf_hooks";
import process from "node:process";
import WebSocket from "ws";

const SAMPLE_RATE = 24_000;
const BYTES_PER_SAMPLE = 2;
const DEFAULT_CHUNK_MS = 100;
const DEFAULT_MANIFEST = "samples/stage1-benchmark.json";
const DEFAULT_REPORT = "runs/realtime-translation-report.json";
const DEFAULT_RUN_DIR = "runs/realtime-translation";
const DEFAULT_SOURCE_DIR = "samples/realtime-pcm";

async function main() {
  const args = parseArgs(process.argv.slice(2));
  await loadDotenv(".env");
  requireEnv("OPENAI_API_KEY");

  const manifest = await readJson(args.manifest);
  const selectedCases = manifest.cases.filter((entry) => {
    if (args.caseId) {
      return entry.id === args.caseId;
    }
    return entry.group === "short";
  });
  if (selectedCases.length === 0) {
    throw new Error(`No benchmark cases selected from ${args.manifest}.`);
  }

  const results = [];
  for (const entry of selectedCases) {
    results.push(await probeCase(entry, args));
  }

  const report = {
    benchmark: "realtime-translation",
    model: "gpt-realtime-translate",
    target_language: args.targetLanguage,
    manifest_path: args.manifest,
    report_path: args.report,
    chunk_ms: args.chunkMs,
    pace: args.pace,
    cases: results,
    summary: summarize(results),
  };
  await writeJson(args.report, report);
  printReport(report);
}

function parseArgs(argv) {
  const options = {
    manifest: DEFAULT_MANIFEST,
    report: DEFAULT_REPORT,
    runDir: DEFAULT_RUN_DIR,
    sourceDir: DEFAULT_SOURCE_DIR,
    targetLanguage: "en",
    chunkMs: DEFAULT_CHUNK_MS,
    pace: "realtime",
    caseId: "",
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = argv[index + 1];
    if (arg === "--manifest") {
      options.manifest = requiredValue(arg, next);
      index += 1;
    } else if (arg === "--report") {
      options.report = requiredValue(arg, next);
      index += 1;
    } else if (arg === "--run-dir") {
      options.runDir = requiredValue(arg, next);
      index += 1;
    } else if (arg === "--source-dir") {
      options.sourceDir = requiredValue(arg, next);
      index += 1;
    } else if (arg === "--target-language") {
      options.targetLanguage = requiredValue(arg, next);
      index += 1;
    } else if (arg === "--case") {
      options.caseId = requiredValue(arg, next);
      index += 1;
    } else if (arg === "--chunk-ms") {
      options.chunkMs = Number.parseInt(requiredValue(arg, next), 10);
      index += 1;
    } else if (arg === "--pace") {
      options.pace = requiredValue(arg, next);
      index += 1;
    } else if (arg === "--help" || arg === "-h") {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!Number.isFinite(options.chunkMs) || options.chunkMs <= 0) {
    throw new Error("--chunk-ms must be a positive integer.");
  }
  if (!["realtime", "fast"].includes(options.pace)) {
    throw new Error("--pace must be either realtime or fast.");
  }
  return options;
}

function requiredValue(flag, value) {
  if (!value || value.startsWith("--")) {
    throw new Error(`${flag} requires a value.`);
  }
  return value;
}

function printHelp() {
  console.log(`Bridge realtime translation probe

Usage:
  node .\\scripts\\probe_realtime_translation.mjs [options]

Options:
  --case ID                 Probe one manifest case instead of all short cases.
  --chunk-ms N              Source PCM chunk size. Default: ${DEFAULT_CHUNK_MS}.
  --pace realtime|fast      Send chunks like live audio or as fast as possible.
  --target-language CODE    Translation output language. Default: en.
  --manifest PATH           Benchmark manifest. Default: ${DEFAULT_MANIFEST}.
  --report PATH             JSON report path. Default: ${DEFAULT_REPORT}.
  --run-dir PATH            Output directory. Default: ${DEFAULT_RUN_DIR}.
  --source-dir PATH         Generated PCM fixture directory. Default: ${DEFAULT_SOURCE_DIR}.
`);
}

async function probeCase(entry, args) {
  const caseDir = path.join(args.runDir, entry.id);
  await fs.mkdir(caseDir, { recursive: true });
  const sourcePcmPath = path.join(args.sourceDir, `${entry.id}.pcm`);
  const sourcePcm = await getSourcePcm(entry, sourcePcmPath);
  const startedAt = performance.now();
  const metrics = {
    first_input_transcript_ms: null,
    first_output_transcript_ms: null,
    first_output_audio_ms: null,
    source_stream_ms: null,
    session_closed_ms: null,
  };
  const events = [];
  const inputTranscript = [];
  const outputTranscript = [];
  const outputAudio = [];
  let sourceStreamFinishedAt = null;

  const socket = await connectTranslationSocket();
  const closePromise = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Translation session close timed out.")), 45_000);

    socket.on("message", (raw) => {
      const event = JSON.parse(raw.toString());
      const eventMs = roundMs(performance.now() - startedAt);
      events.push({ type: event.type, at_ms: eventMs });

      if (event.type === "session.input_transcript.delta") {
        metrics.first_input_transcript_ms ??= eventMs;
        inputTranscript.push(event.delta ?? "");
      }
      if (event.type === "session.output_transcript.delta") {
        metrics.first_output_transcript_ms ??= eventMs;
        outputTranscript.push(event.delta ?? "");
      }
      if (event.type === "session.output_audio.delta") {
        metrics.first_output_audio_ms ??= eventMs;
        outputAudio.push(Buffer.from(event.delta ?? "", "base64"));
      }
      if (event.type === "error") {
        clearTimeout(timer);
        reject(new Error(`Realtime translation error: ${JSON.stringify(event.error ?? event)}`));
      }
      if (event.type === "session.closed") {
        metrics.session_closed_ms = eventMs;
        clearTimeout(timer);
        resolve();
      }
    });
    socket.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
  });

  socket.send(
    JSON.stringify({
      type: "session.update",
      session: {
        audio: {
          output: {
            language: args.targetLanguage,
          },
        },
      },
    }),
  );

  await streamPcm(socket, sourcePcm, args.chunkMs, args.pace);
  sourceStreamFinishedAt = performance.now();
  metrics.source_stream_ms = roundMs(sourceStreamFinishedAt - startedAt);
  socket.send(JSON.stringify({ type: "session.close" }));
  await closePromise;
  socket.close();

  const outputPcm = Buffer.concat(outputAudio);
  const outputPcmPath = path.join(caseDir, "translated-output.pcm");
  const outputWavPath = path.join(caseDir, "translated-output.wav");
  await fs.writeFile(outputPcmPath, outputPcm);
  await fs.writeFile(outputWavPath, wrapPcm16Wav(outputPcm));
  await fs.writeFile(path.join(caseDir, "input-transcript.txt"), inputTranscript.join(""));
  await fs.writeFile(path.join(caseDir, "output-transcript.txt"), outputTranscript.join(""));
  await writeJson(path.join(caseDir, "events.json"), events);

  return {
    id: entry.id,
    expected_spanish: entry.expected_spanish,
    source_pcm_path: sourcePcmPath,
    source_audio_ms: pcmDurationMs(sourcePcm),
    input_transcript: inputTranscript.join(""),
    output_transcript: outputTranscript.join(""),
    output_audio_path: outputWavPath,
    output_audio_bytes: outputPcm.length,
    metrics,
  };
}

async function connectTranslationSocket() {
  const socket = new WebSocket(
    "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate",
    {
      headers: {
        Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
        "OpenAI-Safety-Identifier": "bridge-stage1-spike",
      },
    },
  );
  await onceOpen(socket);
  return socket;
}

function onceOpen(socket) {
  return new Promise((resolve, reject) => {
    socket.once("open", resolve);
    socket.once("error", reject);
  });
}

async function streamPcm(socket, pcm, chunkMs, pace) {
  const bytesPerChunk = Math.max(2, Math.floor((SAMPLE_RATE * BYTES_PER_SAMPLE * chunkMs) / 1000));
  for (let offset = 0; offset < pcm.length; offset += bytesPerChunk) {
    const chunk = pcm.subarray(offset, Math.min(offset + bytesPerChunk, pcm.length));
    socket.send(
      JSON.stringify({
        type: "session.input_audio_buffer.append",
        audio: chunk.toString("base64"),
      }),
    );
    if (pace === "realtime") {
      await sleep(chunkMs);
    }
  }
}

async function getSourcePcm(entry, sourcePcmPath) {
  try {
    return await fs.readFile(sourcePcmPath);
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }

  if (!entry.expected_spanish) {
    throw new Error(`Manifest case ${entry.id} needs expected_spanish to generate PCM.`);
  }
  await fs.mkdir(path.dirname(sourcePcmPath), { recursive: true });
  const response = await fetch("https://api.openai.com/v1/audio/speech", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: process.env.OPENAI_TTS_MODEL || "gpt-4o-mini-tts",
      voice: process.env.OPENAI_TTS_VOICE || "coral",
      input: entry.expected_spanish,
      instructions: "Speak this Spanish customer-service utterance clearly and naturally.",
      response_format: "pcm",
    }),
  });
  if (!response.ok) {
    throw new Error(`OpenAI PCM fixture generation failed (${response.status}): ${await response.text()}`);
  }
  const pcm = Buffer.from(await response.arrayBuffer());
  await fs.writeFile(sourcePcmPath, pcm);
  return pcm;
}

function wrapPcm16Wav(pcm) {
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + pcm.length, 4);
  header.write("WAVE", 8);
  header.write("fmt ", 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(SAMPLE_RATE, 24);
  header.writeUInt32LE(SAMPLE_RATE * BYTES_PER_SAMPLE, 28);
  header.writeUInt16LE(BYTES_PER_SAMPLE, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}

function pcmDurationMs(pcm) {
  return roundMs((pcm.length / (SAMPLE_RATE * BYTES_PER_SAMPLE)) * 1000);
}

function summarize(results) {
  return {
    cases: results.length,
    first_output_audio_p50_ms: medianMetric(results, "first_output_audio_ms"),
    first_output_transcript_p50_ms: medianMetric(results, "first_output_transcript_ms"),
    session_closed_p50_ms: medianMetric(results, "session_closed_ms"),
  };
}

function medianMetric(results, key) {
  const values = results
    .map((entry) => entry.metrics[key])
    .filter((value) => Number.isFinite(value))
    .sort((left, right) => left - right);
  if (values.length === 0) {
    return null;
  }
  const midpoint = Math.floor(values.length / 2);
  if (values.length % 2 === 1) {
    return values[midpoint];
  }
  return roundMs((values[midpoint - 1] + values[midpoint]) / 2);
}

function printReport(report) {
  console.log("Bridge realtime translation probe complete");
  console.log(`model:        ${report.model}`);
  console.log(`target:       ${report.target_language}`);
  console.log(`pace:         ${report.pace}`);
  console.log(`chunk ms:     ${report.chunk_ms}`);
  console.log(`report:       ${report.report_path}`);
  console.log("");
  for (const entry of report.cases) {
    console.log(`- ${entry.id}:`);
    console.log(`  source audio:          ${entry.source_audio_ms} ms`);
    console.log(`  first transcript:      ${formatMetric(entry.metrics.first_output_transcript_ms)}`);
    console.log(`  first translated audio:${formatMetric(entry.metrics.first_output_audio_ms)}`);
    console.log(`  session closed:        ${formatMetric(entry.metrics.session_closed_ms)}`);
    console.log(`  output transcript:     ${entry.output_transcript || "(none)"}`);
  }
  console.log("");
  console.log("summary:");
  console.log(`- first translated audio p50: ${formatMetric(report.summary.first_output_audio_p50_ms)}`);
  console.log(`- first transcript p50:        ${formatMetric(report.summary.first_output_transcript_p50_ms)}`);
  console.log(`- session closed p50:          ${formatMetric(report.summary.session_closed_p50_ms)}`);
}

function formatMetric(value) {
  return Number.isFinite(value) ? ` ${value} ms` : " n/a";
}

function roundMs(value) {
  return Math.round(value * 100) / 100;
}

function sleep(durationMs) {
  return new Promise((resolve) => setTimeout(resolve, durationMs));
}

async function readJson(filePath) {
  return JSON.parse(await fs.readFile(filePath, "utf8"));
}

async function writeJson(filePath, value) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

async function loadDotenv(filePath) {
  let content = "";
  try {
    content = await fs.readFile(filePath, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") {
      return;
    }
    throw error;
  }
  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) {
      continue;
    }
    const [key, ...parts] = line.split("=");
    if (!process.env[key]) {
      process.env[key] = parts.join("=").trim().replace(/^['"]|['"]$/g, "");
    }
  }
}

function requireEnv(key) {
  if (!process.env[key]) {
    throw new Error(`${key} is required. Add it to .env before running the realtime probe.`);
  }
}

main().catch((error) => {
  console.error(`Bridge realtime translation probe failed\n${error.stack || error.message}`);
  process.exitCode = 1;
});
