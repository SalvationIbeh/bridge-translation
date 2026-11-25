const startButton = document.querySelector("#startButton");
const stopButton = document.querySelector("#stopButton");
const armButton = document.querySelector("#armButton");
const resetButton = document.querySelector("#resetButton");
const clearTranscriptButton = document.querySelector("#clearTranscriptButton");
const targetLanguage = document.querySelector("#targetLanguage");
const connectionState = document.querySelector("#connectionState");
const metricState = document.querySelector("#metricState");
const translatedAudio = document.querySelector("#translatedAudio");
const transcript = document.querySelector("#transcript");
const eventsList = document.querySelector("#events");
const inputMeter = document.querySelector("#inputMeter");
const outputMeter = document.querySelector("#outputMeter");
const inputReadout = document.querySelector("#inputReadout");
const outputReadout = document.querySelector("#outputReadout");
const speechMetric = document.querySelector("#speechMetric");
const audioMetric = document.querySelector("#audioMetric");
const transcriptMetric = document.querySelector("#transcriptMetric");

const INPUT_THRESHOLD = 0.025;
const OUTPUT_THRESHOLD = 0.018;
const METER_FLOOR = 0.004;

let peerConnection = null;
let eventChannel = null;
let sourceStream = null;
let remoteStream = null;
let audioContext = null;
let inputAnalyser = null;
let outputAnalyser = null;
let meterFrame = 0;
let phraseArmed = false;
let phraseSpeechAt = null;
let firstTranslatedAudioAt = null;
let firstTranscriptAt = null;

startButton.addEventListener("click", startSession);
stopButton.addEventListener("click", stopSession);
armButton.addEventListener("click", armPhrase);
resetButton.addEventListener("click", resetMetrics);
clearTranscriptButton.addEventListener("click", () => {
  transcript.textContent = "";
});

async function startSession() {
  setConnection("Connecting", "working");
  startButton.disabled = true;
  targetLanguage.disabled = true;
  transcript.textContent = "";
  eventsList.replaceChildren();

  try {
    const clientSecret = await createClientSecret(targetLanguage.value);
    sourceStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    peerConnection = new RTCPeerConnection();
    sourceStream.getAudioTracks().forEach((track) => {
      peerConnection.addTrack(track, sourceStream);
    });
    peerConnection.addEventListener("connectionstatechange", () => {
      const state = peerConnection?.connectionState || "closed";
      if (state === "connected") {
        setConnection("Connected", "good");
      } else if (state === "failed" || state === "disconnected") {
        setConnection(state, "bad");
      } else {
        setConnection(state, "working");
      }
    });
    peerConnection.addEventListener("track", ({ streams }) => {
      remoteStream = streams[0];
      translatedAudio.srcObject = remoteStream;
      translatedAudio.play().catch(() => {
        addEvent("Audio autoplay waiting for the browser.");
      });
      attachOutputMeter(remoteStream);
      addEvent("Translated audio track ready.");
    });

    eventChannel = peerConnection.createDataChannel("oai-events");
    eventChannel.addEventListener("open", () => addEvent("Realtime event channel open."));
    eventChannel.addEventListener("message", ({ data }) => handleRealtimeEvent(data));
    eventChannel.addEventListener("close", () => addEvent("Realtime event channel closed."));

    attachInputMeter(sourceStream);
    await audioContext.resume();
    const offer = await peerConnection.createOffer();
    await peerConnection.setLocalDescription(offer);
    const answer = await fetch("https://api.openai.com/v1/realtime/translations/calls", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${clientSecret}`,
        "Content-Type": "application/sdp",
      },
      body: offer.sdp,
    });
    if (!answer.ok) {
      throw new Error(await answer.text());
    }
    await peerConnection.setRemoteDescription({
      type: "answer",
      sdp: await answer.text(),
    });
    stopButton.disabled = false;
    armButton.disabled = false;
    resetButton.disabled = false;
    addEvent(`Session created for ${targetLanguage.selectedOptions[0].text}.`);
  } catch (error) {
    setConnection("Error", "bad");
    addEvent(error.message || "Could not start the live probe.");
    await stopSession({ keepEvent: true });
  }
}

async function createClientSecret(language) {
  const response = await fetch("/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ targetLanguage: language }),
  });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error || "Could not create a translation client secret.");
  }
  if (!body.value) {
    throw new Error("Translation client secret response did not include a value.");
  }
  return body.value;
}

function armPhrase() {
  resetMetrics();
  phraseArmed = true;
  setMetricState("Listening", "working");
  addEvent("Phrase armed.");
}

function resetMetrics() {
  phraseArmed = false;
  phraseSpeechAt = null;
  firstTranslatedAudioAt = null;
  firstTranscriptAt = null;
  speechMetric.textContent = "n/a";
  audioMetric.textContent = "n/a";
  transcriptMetric.textContent = "n/a";
  setMetricState("Metrics reset", "idle");
}

async function stopSession(options = {}) {
  phraseArmed = false;
  window.cancelAnimationFrame(meterFrame);
  inputMeter.style.width = "0%";
  outputMeter.style.width = "0%";
  inputReadout.textContent = "Waiting";
  outputReadout.textContent = "Waiting";
  inputAnalyser = null;
  outputAnalyser = null;
  eventChannel?.close();
  peerConnection?.close();
  sourceStream?.getTracks().forEach((track) => track.stop());
  remoteStream?.getTracks().forEach((track) => track.stop());
  translatedAudio.srcObject = null;
  if (audioContext) {
    await audioContext.close();
  }
  eventChannel = null;
  peerConnection = null;
  sourceStream = null;
  remoteStream = null;
  audioContext = null;
  startButton.disabled = false;
  targetLanguage.disabled = false;
  stopButton.disabled = true;
  armButton.disabled = true;
  resetButton.disabled = true;
  if (!options.keepEvent) {
    addEvent("Session stopped.");
  }
  setConnection("Idle", "idle");
  setMetricState("Metrics off", "idle");
}

function handleRealtimeEvent(rawEvent) {
  const event = JSON.parse(rawEvent);
  if (event.type === "session.output_transcript.delta") {
    transcript.textContent += event.delta || "";
    if (phraseSpeechAt && firstTranscriptAt === null) {
      firstTranscriptAt = performance.now();
      transcriptMetric.textContent = elapsedFromSpeech(firstTranscriptAt);
      addEvent(`First transcript delta ${transcriptMetric.textContent}.`);
    }
  }
  if (event.type === "error") {
    addEvent(event.error?.message || "Realtime session error.");
  }
}

function attachInputMeter(stream) {
  const { analyser, context } = createAnalyser(stream);
  audioContext = context;
  inputAnalyser = analyser;
  runMeters();
}

function attachOutputMeter(stream) {
  if (!audioContext) {
    return;
  }
  const source = audioContext.createMediaStreamSource(stream);
  outputAnalyser = audioContext.createAnalyser();
  outputAnalyser.fftSize = 1024;
  const mute = audioContext.createGain();
  mute.gain.value = 0;
  source.connect(outputAnalyser);
  outputAnalyser.connect(mute);
  mute.connect(audioContext.destination);
}

function createAnalyser(stream) {
  const context = new AudioContext();
  const source = context.createMediaStreamSource(stream);
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  const mute = context.createGain();
  mute.gain.value = 0;
  source.connect(analyser);
  analyser.connect(mute);
  mute.connect(context.destination);
  return { analyser, context };
}

function runMeters() {
  const inputLevel = measureLevel(inputAnalyser);
  const outputLevel = measureLevel(outputAnalyser);
  renderMeter(inputMeter, inputReadout, inputLevel);
  renderMeter(outputMeter, outputReadout, outputLevel);

  if (phraseArmed && phraseSpeechAt === null && inputLevel >= INPUT_THRESHOLD) {
    phraseSpeechAt = performance.now();
    phraseArmed = false;
    speechMetric.textContent = "0 ms";
    setMetricState("Timing", "working");
    addEvent("Input speech detected.");
  }
  if (phraseSpeechAt && firstTranslatedAudioAt === null && outputLevel >= OUTPUT_THRESHOLD) {
    firstTranslatedAudioAt = performance.now();
    audioMetric.textContent = elapsedFromSpeech(firstTranslatedAudioAt);
    setMetricState("Measured", "good");
    addEvent(`First translated audio ${audioMetric.textContent}.`);
  }
  meterFrame = window.requestAnimationFrame(runMeters);
}

function measureLevel(analyser) {
  if (!analyser) {
    return 0;
  }
  const frame = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(frame);
  let sum = 0;
  for (const sample of frame) {
    sum += sample * sample;
  }
  return Math.sqrt(sum / frame.length);
}

function renderMeter(element, readout, value) {
  const visibleLevel = Math.max(0, Math.min(1, (value - METER_FLOOR) * 18));
  element.style.width = `${Math.round(visibleLevel * 100)}%`;
  readout.textContent = value > METER_FLOOR ? value.toFixed(3) : "Quiet";
}

function elapsedFromSpeech(instant) {
  return `${Math.round(instant - phraseSpeechAt)} ms`;
}

function setConnection(text, tone) {
  connectionState.textContent = text;
  connectionState.dataset.tone = tone;
}

function setMetricState(text, tone) {
  metricState.textContent = text;
  metricState.dataset.tone = tone;
}

function addEvent(message) {
  const item = document.createElement("li");
  item.textContent = `${timestamp()} ${message}`;
  eventsList.prepend(item);
  while (eventsList.children.length > 8) {
    eventsList.lastElementChild.remove();
  }
}

function timestamp() {
  return new Intl.DateTimeFormat([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date());
}
