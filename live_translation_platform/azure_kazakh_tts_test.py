import azure.cognitiveservices.speech as speechsdk

AZURE_SPEECH_REGION = "westeurope"  # замени на свой регион, например westeurope

TEXT = """
Сәлеметсіз бе! Бұл қазақ тіліндегі мәтінді дыбыстау сынағы.
Бүгін біз Azure арқылы қазақша Text-to-Speech тексеріп жатырмыз.
"""

OUTPUT_FILE = "kazakh_tes3t.wav"

speech_config = speechsdk.SpeechConfig(
    subscription=AZURE_SPEECH_KEY,
    region=AZURE_SPEECH_REGION
)

#speech_config.speech_synthesis_voice_name = "kk-KZ-AigulNeural"
# Мужской голос:
speech_config.speech_synthesis_voice_name = "kk-KZ-DauletNeural"

audio_config = speechsdk.audio.AudioOutputConfig(filename=OUTPUT_FILE)

synthesizer = speechsdk.SpeechSynthesizer(
    speech_config=speech_config,
    audio_config=audio_config
)

result = synthesizer.speak_text_async(TEXT).get()

if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
    print(f"Готово: {OUTPUT_FILE}")

elif result.reason == speechsdk.ResultReason.Canceled:
    details = result.cancellation_details
    print("Ошибка:")
    print("Reason:", details.reason)
    print("Error details:", details.error_details)