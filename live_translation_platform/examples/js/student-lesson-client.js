export class StudentLessonClient {
  constructor({ lessonId, token, captionsWebSocketUrl, questionsWebSocketUrl, ttsSynthesizeUrl, textQuestionUrl }) {
    this.lessonId = lessonId;
    this.token = token;
    this.captionsWebSocketUrl = captionsWebSocketUrl;
    this.questionsWebSocketUrl = questionsWebSocketUrl;
    this.ttsSynthesizeUrl = ttsSynthesizeUrl;
    this.textQuestionUrl = textQuestionUrl;
  }

  connectCaptions(onCaption) {
    return this.#connectJsonSocket(this.captionsWebSocketUrl, (event) => {
      if (event.event === "caption") onCaption(event);
    });
  }

  connectQuestions(onQuestionEvent) {
    return this.#connectJsonSocket(this.questionsWebSocketUrl, onQuestionEvent);
  }

  async synthesizeTts({ text, language, provider = "mock", voice, captionId, sequence }) {
    const response = await fetch(this.ttsSynthesizeUrl, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        text,
        language,
        provider,
        voice,
        caption_id: captionId,
        sequence,
      }),
    });
    if (!response.ok) throw new Error(await response.text());
    return {
      audioBlob: await response.blob(),
      provider: response.headers.get("x-tts-provider"),
      language: response.headers.get("x-tts-language"),
      cached: response.headers.get("x-tts-cached") === "true",
      latencyMs: Number(response.headers.get("x-tts-latency-ms") || 0),
    };
  }

  async sendTextQuestion({ studentId, studentName, sourceLanguage, text }) {
    const response = await fetch(this.textQuestionUrl, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        student_id: studentId,
        student_name: studentName,
        source_language: sourceLanguage,
        text,
      }),
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  #connectJsonSocket(url, onEvent) {
    const socket = new WebSocket(url);
    socket.onmessage = (message) => onEvent(JSON.parse(message.data));
    return socket;
  }
}

// Example:
// const client = new StudentLessonClient(studentTokenResponse);
// client.connectCaptions((caption) => console.log(caption.translations?.kk));
// client.connectQuestions((event) => console.log(event.event, event.question));
// await client.synthesizeTts({text: "Сәлем", language: "kk"});
// await client.sendTextQuestion({studentId: "student-123", studentName: "Aidos", sourceLanguage: "kk", text: "Массив деген не?"});
