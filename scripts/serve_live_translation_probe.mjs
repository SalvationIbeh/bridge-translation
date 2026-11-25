import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PUBLIC_DIR = path.join(ROOT, "live_probe");
const PORT = Number.parseInt(process.env.BRIDGE_LIVE_PORT || "8787", 10);
const STATIC_FILES = new Map([
  ["/", ["index.html", "text/html; charset=utf-8"]],
  ["/app.js", ["app.js", "text/javascript; charset=utf-8"]],
  ["/styles.css", ["styles.css", "text/css; charset=utf-8"]],
]);

await loadDotenv(path.join(ROOT, ".env"));

const server = http.createServer(async (request, response) => {
  try {
    if (request.method === "GET" && request.url === "/health") {
      return sendJson(response, 200, {
        ok: true,
        openaiKeyConfigured: Boolean(process.env.OPENAI_API_KEY),
      });
    }
    if (request.method === "POST" && request.url === "/session") {
      return createTranslationSession(request, response);
    }
    if (request.method === "GET" && STATIC_FILES.has(request.url)) {
      const [fileName, contentType] = STATIC_FILES.get(request.url);
      return sendFile(response, path.join(PUBLIC_DIR, fileName), contentType);
    }
    return sendText(response, 404, "Not found");
  } catch (error) {
    console.error(error);
    return sendJson(response, 500, { error: error.message || "Server error" });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`Bridge live translation probe listening on http://127.0.0.1:${PORT}`);
});

async function createTranslationSession(request, response) {
  if (!process.env.OPENAI_API_KEY) {
    return sendJson(response, 500, {
      error: "OPENAI_API_KEY is missing. Add it to .env before starting the live probe.",
    });
  }
  const body = await readJsonBody(request);
  const targetLanguage = normalizeTargetLanguage(body.targetLanguage);
  const upstream = await fetch("https://api.openai.com/v1/realtime/translations/client_secrets", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
      "Content-Type": "application/json",
      "OpenAI-Safety-Identifier": "bridge-live-mic-probe",
    },
    body: JSON.stringify({
      session: {
        model: "gpt-realtime-translate",
        audio: {
          output: {
            language: targetLanguage,
          },
        },
      },
    }),
  });
  const payload = await upstream.text();
  response.writeHead(upstream.status, {
    "Content-Type": upstream.headers.get("content-type") || "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  response.end(payload);
}

function normalizeTargetLanguage(value) {
  return value === "es" ? "es" : "en";
}

async function readJsonBody(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  if (chunks.length === 0) {
    return {};
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function sendFile(response, filePath, contentType) {
  const content = await fs.readFile(filePath);
  response.writeHead(200, {
    "Content-Type": contentType,
    "Cache-Control": "no-store",
  });
  response.end(content);
}

function sendJson(response, status, value) {
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  response.end(JSON.stringify(value));
}

function sendText(response, status, text) {
  response.writeHead(status, {
    "Content-Type": "text/plain; charset=utf-8",
    "Cache-Control": "no-store",
  });
  response.end(text);
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
    const [key, ...valueParts] = line.split("=");
    if (!process.env[key]) {
      process.env[key] = valueParts.join("=").trim().replace(/^['"]|['"]$/g, "");
    }
  }
}
