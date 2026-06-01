from app.translation.base import TranslationProvider


TRANSLATIONS = {
    "Сегодня мы изучим переменные в C#.": {
        "kk": "Бүгін біз C# тіліндегі айнымалыларды үйренеміз.",
        "uz": "Bugun biz C# dagi o'zgaruvchilarni o'rganamiz.",
        "zh-Hans": "今天我们将学习 C# 中的变量。",
    },
    "Теперь рассмотрим циклы for и while.": {
        "kk": "Енді for және while циклдерін қарастырамыз.",
        "uz": "Endi for va while sikllarini ko'rib chiqamiz.",
        "zh-Hans": "现在我们来看 for 和 while 循环。",
    },
    "Давайте создадим простой массив.": {
        "kk": "Қарапайым массив құрайық.",
        "uz": "Keling, oddiy massiv yaratamiz.",
        "zh-Hans": "让我们创建一个简单数组。",
    },
    "Следующий пример показывает работу функции.": {
        "kk": "Келесі мысал функцияның жұмысын көрсетеді.",
        "uz": "Keyingi misol funksiyaning ishlashini ko'rsatadi.",
        "zh-Hans": "下一个示例展示函数如何工作。",
    },
    "Обратите внимание на тип данных string.": {
        "kk": "string деректер түріне назар аударыңыз.",
        "uz": "string ma'lumot turiga e'tibor bering.",
        "zh-Hans": "请注意 string 数据类型。",
    },
    "Сейчас я объясню, как работает класс.": {
        "kk": "Қазір мен класстың қалай жұмыс істейтінін түсіндіремін.",
        "uz": "Hozir men klass qanday ishlashini tushuntiraman.",
        "zh-Hans": "现在我会解释类是如何工作的。",
    },
    "В конце урока мы решим практическую задачу.": {
        "kk": "Сабақ соңында біз практикалық тапсырма орындаймыз.",
        "uz": "Dars oxirida biz amaliy masalani yechamiz.",
        "zh-Hans": "课程结束时我们会完成一道实践题。",
    },
}


class MockTranslator(TranslationProvider):
    name = "mock"

    async def translate_many(
        self,
        text: str,
        source_language: str,
        target_languages: list[str],
    ) -> dict[str, str]:
        prepared = TRANSLATIONS.get(text)
        if prepared is None:
            prepared = {
                "kk": f"[kk mock] {text}",
                "uz": f"[uz mock] {text}",
                "zh-Hans": f"[zh-Hans mock] {text}",
            }
        return {language: prepared.get(language, f"[{language} mock] {text}") for language in target_languages}

