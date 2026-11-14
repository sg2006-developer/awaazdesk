// Runs on the audio thread. Converts the mic to 16 kHz mono PCM16 and posts
// 40 ms frames (1,280 bytes) plus a level for the meter. The server re-chunks
// frames into the exact 1,024-byte frames that Gnani Prisma streaming expects.
//
// Downsampling without a low-pass filter folds 8-24 kHz energy back into the
// speech band (aliasing), which badly hurts recognition. So when the context
// runs above 16 kHz we low-pass with a windowed-sinc FIR first, then resample.
const TARGET_RATE = 16000;
const FRAME_SAMPLES = 640;
const TAPS = 63;

function lowPass(cutoffRatio) {
  const middle = (TAPS - 1) / 2;
  const kernel = new Float32Array(TAPS);
  let sum = 0;
  for (let i = 0; i < TAPS; i++) {
    const x = i - middle;
    const sinc = x === 0 ? 2 * cutoffRatio : Math.sin(2 * Math.PI * cutoffRatio * x) / (Math.PI * x);
    const blackman = 0.42 - 0.5 * Math.cos((2 * Math.PI * i) / (TAPS - 1)) + 0.08 * Math.cos((4 * Math.PI * i) / (TAPS - 1));
    kernel[i] = sinc * blackman;
    sum += kernel[i];
  }
  return kernel.map(value => value / sum);
}

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    // Cut off a little below the new Nyquist (8 kHz) to leave room for the transition band.
    this.kernel = this.ratio > 1 ? lowPass((0.45 * TARGET_RATE) / sampleRate) : null;
    this.history = new Float32Array(TAPS);
    this.cursor = 0;
    this.position = 0;          // Fractional read position into the filtered stream.
    this.previous = 0;          // Last filtered sample, for interpolation across blocks.
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.filled = 0;
    this.energy = 0;
  }

  filter(sample) {
    if (!this.kernel) return sample;
    this.history[this.cursor] = sample;
    let output = 0;
    for (let i = 0, index = this.cursor; i < TAPS; i++) {
      output += this.kernel[i] * this.history[index];
      index = index === 0 ? TAPS - 1 : index - 1;
    }
    this.cursor = (this.cursor + 1) % TAPS;
    return output;
  }

  emit(sample) {
    const clamped = Math.max(-1, Math.min(1, sample));
    this.frame[this.filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    this.energy += clamped * clamped;
    if (this.filled === FRAME_SAMPLES) {
      const level = Math.sqrt(this.energy / FRAME_SAMPLES);
      this.port.postMessage({pcm: this.frame.buffer, level}, [this.frame.buffer]);
      this.frame = new Int16Array(FRAME_SAMPLES);
      this.filled = 0;
      this.energy = 0;
    }
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    const filtered = new Float32Array(input.length);
    for (let i = 0; i < input.length; i++) filtered[i] = this.filter(input[i]);
    while (this.position < filtered.length) {
      const index = Math.floor(this.position);
      const fraction = this.position - index;
      const before = index === 0 ? this.previous : filtered[index - 1];
      this.emit(before + (filtered[index] - before) * fraction);
      this.position += this.ratio;
    }
    this.position -= filtered.length;
    this.previous = filtered[filtered.length - 1];
    return true;
  }
}

registerProcessor('mic-processor', MicProcessor);
