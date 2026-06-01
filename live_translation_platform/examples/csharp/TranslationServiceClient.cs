using System.Net.Http.Json;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace LiveTranslationIntegration;

public sealed class TranslationServiceClient
{
    private readonly HttpClient _http;
    private readonly string _apiKey;

    public TranslationServiceClient(HttpClient http, string apiKey)
    {
        _http = http;
        _apiKey = apiKey;
    }

    public async Task<IntegrationLessonResponse> CreateLessonAsync(IntegrationLessonCreate request, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Post, "/api/v1/integration/lessons")
        {
            Content = JsonContent.Create(request)
        };
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        return (await response.Content.ReadFromJsonAsync<IntegrationLessonResponse>(cancellationToken: cancellationToken))!;
    }

    public async Task ArmRtmsAsync(string lessonId, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Post, $"/api/v1/integration/lessons/{lessonId}/arm-rtms");
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
    }

    public async Task<IntegrationStatusResponse> GetStatusAsync(string lessonId, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Get, $"/api/v1/integration/lessons/{lessonId}/status");
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        return (await response.Content.ReadFromJsonAsync<IntegrationStatusResponse>(cancellationToken: cancellationToken))!;
    }

    public async Task<StudentTokenResponse> CreateStudentTokenAsync(string lessonId, StudentTokenRequest request, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Post, $"/api/v1/integration/lessons/{lessonId}/student-token")
        {
            Content = JsonContent.Create(request)
        };
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        return (await response.Content.ReadFromJsonAsync<StudentTokenResponse>(cancellationToken: cancellationToken))!;
    }

    public async Task<TeacherTokenResponse> CreateTeacherTokenAsync(string lessonId, TeacherTokenRequest request, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Post, $"/api/v1/integration/lessons/{lessonId}/teacher-token")
        {
            Content = JsonContent.Create(request)
        };
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        return (await response.Content.ReadFromJsonAsync<TeacherTokenResponse>(cancellationToken: cancellationToken))!;
    }

    public async Task<TtsStatusResponse> GetTtsStatusAsync(string lessonId, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Get, $"/api/v1/integration/lessons/{lessonId}/tts/status");
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        return (await response.Content.ReadFromJsonAsync<TtsStatusResponse>(cancellationToken: cancellationToken))!;
    }

    public async Task<TtsAudioResponse> SynthesizeTtsAsync(string lessonId, TtsSynthesizeRequest request, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Post, $"/api/v1/integration/lessons/{lessonId}/tts/synthesize")
        {
            Content = JsonContent.Create(request)
        };
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        var headers = response.Headers.Concat(response.Content.Headers).ToDictionary(item => item.Key, item => string.Join(",", item.Value));
        return new TtsAudioResponse(await response.Content.ReadAsByteArrayAsync(cancellationToken), response.Content.Headers.ContentType?.MediaType, headers);
    }

    public async Task<QuestionResponse> SendTextQuestionAsync(string lessonId, TextQuestionRequest request, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Post, $"/api/v1/integration/lessons/{lessonId}/questions/text")
        {
            Content = JsonContent.Create(request)
        };
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        return (await response.Content.ReadFromJsonAsync<QuestionResponse>(cancellationToken: cancellationToken))!;
    }

    public async Task<QuestionListResponse> ListQuestionsAsync(string lessonId, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Get, $"/api/v1/integration/lessons/{lessonId}/questions");
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        return (await response.Content.ReadFromJsonAsync<QuestionListResponse>(cancellationToken: cancellationToken))!;
    }

    public Task<QuestionResponse> MarkQuestionAnsweredAsync(string lessonId, int questionId, CancellationToken cancellationToken = default)
    {
        return ModerateQuestionAsync(lessonId, questionId, "answer", cancellationToken);
    }

    public Task<QuestionResponse> DismissQuestionAsync(string lessonId, int questionId, CancellationToken cancellationToken = default)
    {
        return ModerateQuestionAsync(lessonId, questionId, "dismiss", cancellationToken);
    }

    private async Task<QuestionResponse> ModerateQuestionAsync(string lessonId, int questionId, string action, CancellationToken cancellationToken)
    {
        using var message = new HttpRequestMessage(HttpMethod.Post, $"/api/v1/integration/lessons/{lessonId}/questions/{questionId}/{action}");
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        return (await response.Content.ReadFromJsonAsync<QuestionResponse>(cancellationToken: cancellationToken))!;
    }

    public async Task<JsonDocument> GetTranscriptAsync(string lessonId, CancellationToken cancellationToken = default)
    {
        using var message = new HttpRequestMessage(HttpMethod.Get, $"/api/v1/integration/lessons/{lessonId}/transcript");
        AddAuth(message);
        using var response = await _http.SendAsync(message, cancellationToken);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken);
        return await JsonDocument.ParseAsync(stream, cancellationToken: cancellationToken);
    }

    public async Task ConnectCaptionsAsync(string captionsWebSocketUrl, Func<CaptionEvent, Task> onCaption, CancellationToken cancellationToken = default)
    {
        using var websocket = new ClientWebSocket();
        await websocket.ConnectAsync(new Uri(captionsWebSocketUrl), cancellationToken);

        var buffer = new byte[64 * 1024];
        while (websocket.State == WebSocketState.Open && !cancellationToken.IsCancellationRequested)
        {
            var result = await websocket.ReceiveAsync(buffer, cancellationToken);
            if (result.MessageType == WebSocketMessageType.Close)
            {
                break;
            }
            var json = Encoding.UTF8.GetString(buffer, 0, result.Count);
            var caption = JsonSerializer.Deserialize<CaptionEvent>(json, JsonOptions.Default);
            if (caption is not null)
            {
                await onCaption(caption);
            }
        }
    }

    private void AddAuth(HttpRequestMessage message)
    {
        message.Headers.Add("X-Integration-Key", _apiKey);
    }
}

public sealed record IntegrationLessonCreate(
    string external_lesson_id,
    string title,
    string mode,
    string stt_provider,
    string translation_provider,
    string[] target_languages,
    bool create_zoom_meeting = true,
    bool glossary_enabled = true,
    string? external_course_id = null,
    string? external_teacher_id = null,
    string? external_tenant_id = null,
    string? glossary_id = null,
    string? callback_url = null
);

public sealed record IntegrationLessonResponse(
    string lesson_id,
    string? external_lesson_id,
    string mode,
    string status,
    ZoomInfo zoom,
    StudentInfo student
);

public sealed record ZoomInfo(string meeting_id, string meeting_uuid, string join_url, string start_url, string password);

public sealed record StudentInfo(string captions_websocket_url, string diagnostics_websocket_url, string embed_config_url);

public sealed record StudentTokenRequest(
    string external_student_id,
    string display_name = "Student",
    string[]? scopes = null,
    int? ttl_seconds = null
);

public sealed record TeacherTokenRequest(
    string external_teacher_id,
    string display_name = "Teacher",
    string[]? scopes = null,
    int? ttl_seconds = null
);

public sealed record StudentTokenResponse(
    string token,
    string expires_at,
    string lesson_id,
    string captions_websocket_url,
    string embed_config_url,
    string tts_status_url,
    string tts_synthesize_url,
    string questions_websocket_url,
    string text_question_url,
    string voice_question_audio_websocket_url
);

public sealed record TeacherTokenResponse(
    string token,
    string expires_at,
    string lesson_id,
    string audio_ingest_websocket_url,
    string diagnostics_websocket_url,
    string questions_websocket_url,
    string questions_list_url,
    string question_answer_url_template,
    string question_dismiss_url_template
);

public sealed record TtsStatusResponse(
    bool enabled,
    string provider,
    string active_provider,
    bool ready,
    string[] missing,
    string[] supported_languages,
    JsonElement voices,
    JsonElement providers
);

public sealed record TtsSynthesizeRequest(
    string text,
    string language,
    string? provider = null,
    string? voice = null,
    string? caption_id = null,
    int? sequence = null
);

public sealed record TtsAudioResponse(byte[] audio_bytes, string? content_type, Dictionary<string, string> headers);

public sealed record TextQuestionRequest(
    string? student_id,
    string? student_name,
    string source_language,
    string text
);

public sealed record QuestionListResponse(string lesson_id, string? external_lesson_id, QuestionResponse[] questions);

public sealed record QuestionResponse(
    int id,
    string lesson_id,
    string? external_lesson_id,
    string? student_id,
    string? student_name,
    string input_type,
    string source_language,
    string original_text,
    string? recognized_text,
    string translated_text_ru,
    string status,
    string? stt_provider,
    string? translation_provider,
    int? audio_duration_ms,
    int? latency_ms,
    string? error,
    string created_at,
    string? answered_at,
    string? dismissed_at
);

public sealed record IntegrationStatusResponse(
    string lesson_id,
    string? external_lesson_id,
    string lesson_status,
    string rtms_status,
    string pipeline_status,
    JsonElement stt,
    JsonElement translation,
    JsonElement captions,
    JsonElement latency_ms
);

public sealed record CaptionEvent(
    [property: JsonPropertyName("event")] string Event,
    string version,
    string lesson_id,
    string? external_lesson_id,
    bool is_final,
    string? original_text_normalized,
    Dictionary<string, string>? translations
);

public static class JsonOptions
{
    public static readonly JsonSerializerOptions Default = new(JsonSerializerDefaults.Web)
    {
        PropertyNameCaseInsensitive = true
    };
}
