class TeacherMicProcessor extends AudioWorkletProcessor {
  process(inputs, outputs) {
    const input = inputs[0];
    if (!input || !input[0] || !input[0].length) return true;
    const output = outputs[0];
    if (output && output[0]) {
      output[0].set(input[0]);
    }
    const chunk = new Float32Array(input[0].length);
    chunk.set(input[0]);
    this.port.postMessage(chunk, [chunk.buffer]);
    return true;
  }
}

registerProcessor("teacher-mic-processor", TeacherMicProcessor);
