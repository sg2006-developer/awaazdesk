import {Player} from './audio.mjs';

/**
 * A hands-free call: mic -> /ws/calls/:id -> Gnani Prisma, Gnani Timbre -> speaker.
 * Emits the server's JSON events to `onEvent`, plus local ones:
 *   {type:'level', mic, agent}  ~20x per second, for the meters
 *   {type:'closed', reason}
 */
export class LiveSession {
  constructor(callId, onEvent) {
    this.callId = callId;
    this.onEvent = onEvent;
    this.muted = false;
    this.bargeIn = true;
    this.micLevel = 0;
  }

  async start() {
    if (!navigator.mediaDevices?.getUserMedia) throw Error('Microphone access requires localhost or HTTPS in a supported browser.');
    // Echo cancellation lets the caller interrupt without the agent hearing itself.
    this.stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
    this.player = new Player();
    await this.player.resume();
    // Prefer letting the browser resample to 16 kHz (high quality); fall back to
    // the worklet's own filtered resampler where a 16 kHz context isn't allowed.
    // (Firefox creates a 16 kHz context but refuses to connect a 48 kHz mic to it.)
    const connect = async context => {
      await context.audioWorklet.addModule('/static/mic-worklet.js');
      const node = new AudioWorkletNode(context, 'mic-processor');
      context.createMediaStreamSource(this.stream).connect(node);
      return node;
    };
    try {
      this.micContext = new AudioContext({sampleRate: 16000});
      this.micNode = await connect(this.micContext);
    } catch {
      this.micContext?.close().catch(() => {});
      this.micContext = new AudioContext();
      this.micNode = await connect(this.micContext);
    }
    this.micNode.port.onmessage = ({data}) => this.onMicFrame(data);

    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    this.ws = new WebSocket(`${scheme}://${location.host}/ws/calls/${this.callId}`);
    this.ws.binaryType = 'arraybuffer';
    this.ws.onmessage = ({data}) => {
      if (data instanceof ArrayBuffer) { this.player.enqueue(data); return; }
      const event = JSON.parse(data);
      if (event.type === 'barge_in') this.player.stop();
      this.onEvent(event);
    };
    this.ws.onclose = event => { this.onEvent({type: 'closed', reason: event.reason}); this.cleanup(); };
    await new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = () => reject(Error('Could not open the live call connection.'));
    });
    this.sendSettings();
    this.meter = setInterval(() => this.onEvent({type: 'level', mic: this.micLevel, agent: this.player.level()}), 50);
  }

  onMicFrame({pcm, level}) {
    this.micLevel = this.muted ? 0 : level;
    if (this.muted || this.ws?.readyState !== WebSocket.OPEN) return;
    // Half-duplex fallback: without barge-in, don't stream while the agent talks
    // (useful on speakers where echo cancellation is weak).
    if (!this.bargeIn && this.player.playing) return;
    this.ws.send(pcm);
  }

  send(event) { if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(event)); }
  sendText(text) { this.player.stop(); this.send({type: 'text', text}); }
  sendSettings() { this.send({type: 'settings', barge_in: this.bargeIn}); }
  setBargeIn(value) { this.bargeIn = value; this.sendSettings(); }
  setMuted(value) { this.muted = value; }
  stopSpeaking() { this.player.stop(); this.send({type: 'stop_speaking'}); }
  end() { this.send({type: 'end'}); }

  cleanup() {
    clearInterval(this.meter);
    this.stream?.getTracks().forEach(track => track.stop());
    this.micContext?.close().catch(() => {});
    this.player?.close().catch(() => {});
    this.stream = this.micContext = null;
  }

  close() { this.ws?.close(); this.cleanup(); }
}
