export const SPEECH_RATE = 16000;

export function encodeWav(samples, sampleRate) {
  const output = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(output);
  const label = (offset, text) => [...text].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
  label(0, 'RIFF'); view.setUint32(4, output.byteLength - 8, true); label(8, 'WAVE');
  label(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  label(36, 'data'); view.setUint32(40, samples.length * 2, true);
  samples.forEach((sample, i) => {
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(44 + 2 * i, clamped * (clamped < 0 ? 32768 : 32767), true);
  });
  return new Blob([output], {type: 'audio/wav'});
}

/** Decode a MediaRecorder blob and re-encode as 16 kHz mono WAV (3x smaller than 48 kHz). */
export async function convertRecording(blob) {
  const context = new AudioContext();
  try {
    const audio = await context.decodeAudioData(await blob.arrayBuffer());
    const length = Math.ceil(audio.duration * SPEECH_RATE);
    const offline = new OfflineAudioContext(1, length, SPEECH_RATE);  // Mixes to mono and resamples.
    const source = offline.createBufferSource();
    source.buffer = audio;
    source.connect(offline.destination);
    source.start();
    const rendered = await offline.startRendering();
    return encodeWav(rendered.getChannelData(0), SPEECH_RATE);
  } finally {
    await context.close();
  }
}

/** Gapless playback of streamed PCM16 chunks, with instant stop for barge-in. */
export class Player {
  constructor() {
    this.context = new AudioContext({sampleRate: SPEECH_RATE});
    this.analyser = this.context.createAnalyser();
    this.analyser.fftSize = 256;
    this.analyser.connect(this.context.destination);
    this.sources = new Set();
    this.endsAt = 0;
  }

  async resume() { if (this.context.state !== 'running') await this.context.resume(); }

  enqueue(arrayBuffer) {
    const pcm = new Int16Array(arrayBuffer);
    if (!pcm.length) return;
    const buffer = this.context.createBuffer(1, pcm.length, SPEECH_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 0x8000;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.analyser);
    // A small lead avoids clicks when the first chunk arrives just after currentTime.
    const start = Math.max(this.context.currentTime + 0.03, this.endsAt);
    source.start(start);
    this.endsAt = start + buffer.duration;
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
  }

  get playing() { return this.context.currentTime < this.endsAt; }

  level() {
    const data = new Uint8Array(this.analyser.frequencyBinCount);
    this.analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (const value of data) sum += ((value - 128) / 128) ** 2;
    return Math.sqrt(sum / data.length);
  }

  stop() {
    for (const source of this.sources) { try { source.stop(); } catch { /* already stopped */ } }
    this.sources.clear();
    this.endsAt = 0;
  }

  async close() { this.stop(); await this.context.close(); }
}
