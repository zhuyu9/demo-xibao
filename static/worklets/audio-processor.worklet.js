class PcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetSampleRate = 16000;
    this.pending = [];
    this.pendingSize = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) {
      return true;
    }

    this.append(input[0]);
    const resampled = this.resample();
    if (resampled.length > 0) {
      const pcm = this.floatTo16BitPCM(resampled);
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }

  append(channelData) {
    this.pending.push(channelData);
    this.pendingSize += channelData.length;
  }

  resample() {
    const input = new Float32Array(this.pendingSize);
    let offset = 0;
    for (const chunk of this.pending) {
      input.set(chunk, offset);
      offset += chunk.length;
    }
    this.pending = [];
    this.pendingSize = 0;

    const inputRate = sampleRate;
    const targetRate = this.targetSampleRate;
    if (inputRate === targetRate) {
      return input;
    }

    const ratio = inputRate / targetRate;
    const outputLength = Math.floor(input.length / ratio);
    const output = new Float32Array(outputLength);
    for (let i = 0; i < outputLength; i++) {
      const position = i * ratio;
      const left = Math.floor(position);
      const right = Math.min(left + 1, input.length - 1);
      const fraction = position - left;
      output[i] = input[left] + (input[right] - input[left]) * fraction;
    }
    return output;
  }

  floatTo16BitPCM(float32) {
    const buffer = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const sample = Math.max(-1, Math.min(1, float32[i]));
      buffer[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    return buffer;
  }
}

registerProcessor("pcm-processor", PcmProcessor);
