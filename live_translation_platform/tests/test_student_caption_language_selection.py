import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


def test_student_caption_renderer_respects_selected_language_without_original_fallback():
    if shutil.which("node") is None:
        pytest.skip("node is required for captions.js renderer test")

    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    script = f"""
    const assert = require("assert");

    function element() {{
      return {{
        dataset: {{}},
        value: "",
        textContent: "",
        hidden: false,
        classList: {{ add() {{}}, remove() {{}} }},
        addEventListener() {{}},
        querySelector() {{ return element(); }},
        prepend() {{}},
      }};
    }}

    global.window = {{ location: {{ search: "", protocol: "http:", host: "testserver" }} }};
    global.document = {{
      querySelector(selector) {{ return selector === ".student-layout" ? null : element(); }},
      querySelectorAll() {{ return []; }},
      createElement() {{ return element(); }},
    }};

    {captions_js}

    const payload = {{
      original_text: "Русский original",
      original_text_raw: "Русский raw",
      original_text_normalized: "Русский normalized",
      translations: {{ kk: "Қазақша", uz: "O'zbekcha", "zh-Hans": "中文" }},
    }};

    selectedLanguage = "kk";
    assert.strictEqual(renderText(payload), "Қазақша");

    selectedLanguage = "uz";
    assert.strictEqual(renderText(payload), "O'zbekcha");

    selectedLanguage = "zh-Hans";
    assert.strictEqual(renderText(payload), "中文");

    selectedLanguage = "ru";
    assert.strictEqual(renderText(payload), "Русский normalized");

    selectedLanguage = "original";
    assert.strictEqual(renderText(payload), "Русский normalized");

    selectedLanguage = "all";
    const allText = renderText(payload);
    assert.match(allText, /RU: Русский normalized/);
    assert.match(allText, /kk: Қазақша/);
    assert.match(allText, /uz: O'zbekcha/);
    assert.match(allText, /zh-Hans: 中文/);

    selectedLanguage = "kk";
    const missingText = renderText({{ ...payload, translations: {{ uz: "O'zbekcha" }} }});
    assert.match(missingText, /Translation unavailable for kk|Перевод пока недоступен/);
    assert.notStrictEqual(missingText, "Русский normalized");
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_caption_language_selector_rerenders_existing_final_caption():
    if shutil.which("node") is None:
        pytest.skip("node is required for captions.js renderer test")

    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    script = f"""
    const assert = require("assert");
    function element() {{
      return {{
        dataset: {{}},
        value: "",
        textContent: "",
        hidden: false,
        handlers: {{}},
        children: {{}},
        prepended: [],
        classList: {{ add() {{}}, remove() {{}} }},
        addEventListener(event, handler) {{ this.handlers[event] = handler; }},
        querySelector(selector) {{
          if (!this.children[selector]) this.children[selector] = element();
          return this.children[selector];
        }},
        prepend(child) {{ this.prepended.unshift(child); }},
      }};
    }}

    const languageSelector = element();
    const finalCaptionList = element();
    const elements = {{
      "#languageSelector": languageSelector,
      "#finalCaptions": finalCaptionList,
    }};

    global.window = {{ location: {{ search: "", protocol: "http:", host: "testserver" }} }};
    global.document = {{
      querySelector(query) {{ return query === ".student-layout" ? null : (elements[query] || element()); }},
      querySelectorAll(query) {{ return query === ".caption-item" ? finalCaptionList.prepended : []; }},
      createElement() {{ return element(); }},
    }};

    {captions_js}

    const payload = {{
      speaker: {{ name: "Teacher" }},
      latency_ms: {{ total: 10 }},
      original_text: "Русский original",
      original_text_normalized: "Русский normalized",
      translations: {{ kk: "Қазақша", uz: "O'zbekcha" }},
    }};

    selectedLanguage = "all";
    addFinalCaption(payload);
    const item = finalCaptionList.prepended[0];
    assert.match(item.querySelector("pre").textContent, /RU: Русский normalized/);

    languageSelector.value = "kk";
    languageSelector.handlers.change({{ target: languageSelector }});

    assert.strictEqual(item.hidden, false);
    assert.strictEqual(item.querySelector("pre").textContent, "Қазақша");
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_caption_renderer_skips_duplicate_final_and_allows_later_repeat():
    if shutil.which("node") is None:
        pytest.skip("node is required for captions.js renderer test")

    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    script = f"""
    const assert = require("assert");
    function element() {{
      return {{
        dataset: {{}},
        value: "",
        textContent: "",
        hidden: false,
        handlers: {{}},
        children: {{}},
        prepended: [],
        classList: {{ add() {{}}, remove() {{}} }},
        addEventListener(event, handler) {{ this.handlers[event] = handler; }},
        querySelector(selector) {{
          if (!this.children[selector]) this.children[selector] = element();
          return this.children[selector];
        }},
        prepend(child) {{ this.prepended.unshift(child); }},
      }};
    }}

    const finalCaptionList = element();
    const elements = {{
      "#finalCaptions": finalCaptionList,
    }};
    let now = 1_000_000;
    Date.now = () => now;
    global.window = {{ location: {{ search: "", protocol: "http:", host: "testserver" }} }};
    global.document = {{
      querySelector(query) {{ return query === ".student-layout" ? null : (elements[query] || element()); }},
      querySelectorAll() {{ return []; }},
      createElement() {{ return element(); }},
    }};

    {captions_js}

    const payload = {{
      lesson_id: "lesson-1",
      caption_id: null,
      speaker: {{ id: "teacher", name: "Teacher" }},
      latency_ms: {{ total: 10 }},
      is_final: true,
      original_text: "same final",
      original_text_normalized: "same final",
      translations: {{ kk: "бірдей" }},
    }};

    addFinalCaption(payload);
    addFinalCaption({{ ...payload, latency_ms: {{ total: 35 }} }});
    assert.strictEqual(finalCaptionList.prepended.length, 1);
    assert.strictEqual(window.CaptionDebug.duplicate_captions_skipped, 1);

    now += 10_000;
    addFinalCaption({{ ...payload, latency_ms: {{ total: 55 }} }});
    assert.strictEqual(finalCaptionList.prepended.length, 2);
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_caption_renderer_treats_missing_translation_as_status_not_card():
    if shutil.which("node") is None:
        pytest.skip("node is required for captions.js renderer test")

    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    script = f"""
    const assert = require("assert");
    function element() {{
      return {{
        dataset: {{}},
        value: "",
        textContent: "",
        hidden: false,
        handlers: {{}},
        children: {{}},
        prepended: [],
        classList: {{ add() {{}}, remove() {{}} }},
        addEventListener(event, handler) {{ this.handlers[event] = handler; }},
        querySelector(selector) {{
          if (!this.children[selector]) this.children[selector] = element();
          return this.children[selector];
        }},
        prepend(child) {{ this.prepended.unshift(child); }},
      }};
    }}

    const finalCaptionList = element();
    const elements = {{"#finalCaptions": finalCaptionList}};
    global.window = {{ location: {{ search: "", protocol: "http:", host: "testserver" }} }};
    global.document = {{
      querySelector(query) {{ return query === ".student-layout" ? null : (elements[query] || element()); }},
      querySelectorAll() {{ return []; }},
      createElement() {{ return element(); }},
    }};

    {captions_js}

    const payload = {{
      lesson_id: "lesson-1",
      caption_id: "caption-missing",
      speaker: {{ id: "teacher", name: "Teacher" }},
      latency_ms: {{ total: 10 }},
      is_final: true,
      original_text: "Russian original",
      original_text_normalized: "Russian normalized",
      translations: {{ uz: "O'zbekcha" }},
    }};

    selectedLanguage = "kk";
    const missingResult = resolveCaptionForLanguage(payload, "kk");
    assert.strictEqual(missingResult.kind, "missing");
    assert.strictEqual(missingResult.isRenderableCaption, false);
    assert.strictEqual(missingResult.isTtsEligible, false);
    assert.strictEqual(missingResult.text, "Translation unavailable for kk");
    assert.notStrictEqual(missingResult.text, "Russian normalized");

    assert.strictEqual(addFinalCaption(payload), false);
    assert.strictEqual(finalCaptionList.prepended.length, 0);
    assert.strictEqual(window.CaptionDebug.translation_status_updates, 1);
    assert.strictEqual(window.CaptionDebug.last_translation_status, "Translation unavailable for kk");

    const partial = resolveCaptionForLanguage({{ ...payload, is_partial: true, is_final: false }}, "kk");
    assert.strictEqual(partial.kind, "waiting");
    assert.strictEqual(partial.text, "Waiting for kk translation...");
    assert.strictEqual(partial.isRenderableCaption, false);
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_caption_renderer_limits_visible_finals_and_history_toggle_does_not_trigger_tts():
    if shutil.which("node") is None:
        pytest.skip("node is required for captions.js visible limit test")

    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    script = f"""
    const assert = require("assert");
    function element() {{
      return {{
        dataset: {{}},
        value: "",
        textContent: "",
        hidden: false,
        handlers: {{}},
        children: {{}},
        prepended: [],
        classList: {{ add() {{}}, remove() {{}} }},
        addEventListener(event, handler) {{ this.handlers[event] = handler; }},
        querySelector(selector) {{
          if (!this.children[selector]) this.children[selector] = element();
          return this.children[selector];
        }},
        prepend(child) {{ this.prepended.unshift(child); }},
      }};
    }}

    const finalCaptionList = element();
    const showHistory = element();
    const visibleCount = element();
    const elements = {{
      "#finalCaptions": finalCaptionList,
      "#showCaptionHistory": showHistory,
      "#visibleCaptionCount": visibleCount,
    }};
    let ttsCalls = 0;
    global.window = {{
      location: {{ search: "", protocol: "http:", host: "testserver" }},
      StudentTTS: {{ onFinalCaptionForTts() {{ ttsCalls += 1; }} }},
    }};
    global.document = {{
      querySelector(query) {{ return query === ".student-layout" ? null : (elements[query] || element()); }},
      querySelectorAll(query) {{ return query === ".caption-item" ? finalCaptionList.prepended : []; }},
      createElement() {{ return element(); }},
    }};

    {captions_js}

    for (let index = 1; index <= 10; index += 1) {{
      addFinalCaption({{
        lesson_id: "lesson-1",
        caption_id: `caption-${{index}}`,
        sequence: index,
        speaker: {{ id: "teacher", name: "Teacher" }},
        latency_ms: {{ total: 10 }},
        is_final: true,
        original_text: `final ${{index}}`,
        original_text_normalized: `final ${{index}}`,
        translations: {{ kk: `kk ${{index}}` }},
      }});
    }}

    assert.strictEqual(finalCaptionList.prepended.length, 10);
    assert.deepStrictEqual(
      finalCaptionList.prepended.filter((item) => !item.hidden).map((item) => item.dataset.captionId),
      ["caption-10", "caption-9", "caption-8", "caption-7", "caption-6", "caption-5", "caption-4", "caption-3"],
    );
    assert.deepStrictEqual(
      finalCaptionList.prepended.filter((item) => item.hidden).map((item) => item.dataset.captionId),
      ["caption-2", "caption-1"],
    );
    assert.match(visibleCount.textContent, /8 of 10/);
    assert.match(showHistory.textContent, /Show history/);

    showHistory.handlers.click();

    assert.strictEqual(finalCaptionList.prepended.filter((item) => !item.hidden).length, 10);
    assert.match(showHistory.textContent, /Show latest/);
    assert.strictEqual(ttsCalls, 0);
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_caption_renderer_language_modes_for_missing_and_available_translations():
    if shutil.which("node") is None:
        pytest.skip("node is required for captions.js renderer test")

    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    script = f"""
    const assert = require("assert");
    function element() {{
      return {{
        dataset: {{}},
        value: "",
        textContent: "",
        hidden: false,
        classList: {{ add() {{}}, remove() {{}} }},
        addEventListener() {{}},
        querySelector() {{ return element(); }},
        prepend() {{}},
      }};
    }}

    global.window = {{ location: {{ search: "", protocol: "http:", host: "testserver" }} }};
    global.document = {{
      querySelector(selector) {{ return selector === ".student-layout" ? null : element(); }},
      querySelectorAll() {{ return []; }},
      createElement() {{ return element(); }},
    }};

    {captions_js}

    const payload = {{
      is_final: true,
      original_text: "Russian original",
      original_text_raw: "Russian raw",
      original_text_normalized: "Russian normalized",
      translations: {{ kk: "Kazakh real", uz: "Translation unavailable for uz" }},
    }};

    assert.strictEqual(resolveCaptionForLanguage(payload, "kk").kind, "text");
    assert.strictEqual(resolveCaptionForLanguage(payload, "kk").text, "Kazakh real");
    assert.strictEqual(resolveCaptionForLanguage(payload, "uz").kind, "error");
    assert.strictEqual(resolveCaptionForLanguage(payload, "uz").isRenderableCaption, false);
    assert.strictEqual(resolveCaptionForLanguage(payload, "zh-Hans").kind, "missing");
    assert.strictEqual(resolveCaptionForLanguage(payload, "zh-Hans").text, "Translation unavailable for zh-Hans");
    assert.notStrictEqual(resolveCaptionForLanguage(payload, "zh-Hans").text, "Russian normalized");
    assert.strictEqual(resolveCaptionForLanguage(payload, "ru").text, "Russian normalized");
    assert.strictEqual(resolveCaptionForLanguage(payload, "original").text, "Russian normalized");

    const all = resolveCaptionForLanguage(payload, "all");
    assert.strictEqual(all.kind, "all");
    assert.match(all.text, /RU: Russian normalized/);
    assert.match(all.text, /kk: Kazakh real/);
    assert.doesNotMatch(all.text, /uz: Translation unavailable for uz/);
    assert.doesNotMatch(all.text, /zh-Hans: Translation unavailable for zh-Hans/);
    assert.deepStrictEqual(all.missingLanguages, ["uz", "zh-Hans"]);
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def _run_node_script(script: str):
    return subprocess.run(["node", "-"], input=textwrap.dedent(script), capture_output=True, text=True, encoding="utf-8", timeout=10)
