using LiveTranslationIntegration;

var serviceUrl = Environment.GetEnvironmentVariable("TRANSLATION_SERVICE_URL") ?? "http://127.0.0.1:8000";
var apiKey = Environment.GetEnvironmentVariable("TRANSLATION_SERVICE_KEY") ?? "dev-key-1";

using var http = new HttpClient { BaseAddress = new Uri(serviceUrl) };
var client = new TranslationServiceClient(http, apiKey);

var lesson = await client.CreateLessonAsync(
    new IntegrationLessonCreate(
        external_lesson_id: $"csharp-demo-{Guid.NewGuid():N}",
        title: "C# Arrays Lesson",
        mode: "mock",
        stt_provider: "mock",
        translation_provider: "mock",
        target_languages: new[] { "kk", "uz", "zh-Hans" },
        create_zoom_meeting: false
    )
);

Console.WriteLine($"Created Python lesson: {lesson.lesson_id}");
Console.WriteLine($"Captions WS: {lesson.student.captions_websocket_url}");

var studentToken = await client.CreateStudentTokenAsync(
    lesson.lesson_id,
    new StudentTokenRequest(external_student_id: "student-demo-1", display_name: "Aidos")
);

Console.WriteLine($"Student captions WS: {studentToken.captions_websocket_url}");
Console.WriteLine($"Student TTS status URL: {studentToken.tts_status_url}");

var status = await client.GetStatusAsync(lesson.lesson_id);
Console.WriteLine($"Status: lesson={status.lesson_status}, rtms={status.rtms_status}, pipeline={status.pipeline_status}");

var ttsStatus = await client.GetTtsStatusAsync(lesson.lesson_id);
Console.WriteLine($"TTS: enabled={ttsStatus.enabled}, provider={ttsStatus.active_provider}");

var question = await client.SendTextQuestionAsync(
    lesson.lesson_id,
    new TextQuestionRequest(
        student_id: "student-demo-1",
        student_name: "Aidos",
        source_language: "kk",
        text: "Massiv degen ne?"
    )
);
Console.WriteLine($"Question {question.id}: {question.translated_text_ru}");

var transcript = await client.GetTranscriptAsync(lesson.lesson_id);
Console.WriteLine($"Transcript JSON root: {transcript.RootElement.ValueKind}");
