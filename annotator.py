"""AI 批注：把随笔逐段交给 GLM 解读 / 提文学建议，组装成「原文 + 下划线批注」文档。

批注文档用 HTML 格式写入妙笔——实测妙笔对 <u> 下划线和 <img> 图片引用
都能完整保留；而 markdown 里内嵌 <u> 会被原样转义，所以必须走 HTML。
"""

import html as html_mod
import json
import re

# ------------------------------------------------------------------ 两种批注模式

MODE_LABELS = ["解读批注", "文学提升建议"]

INTERPRET_PROMPT = """\
你是一位敏锐、温暖又诚实的中文散文读者，像一位懂我的老朋友在书页边写字。
我会给你一篇按段落编号的随笔，请你做「解读批注」。

批注原则：
- 每条批注一两句话，直接说到点子上，不绕弯子
- 解读这段写了什么、藏着什么情绪或心事，点出最打动人的细节，以及它为什么好
- 语气自然、具体、有人情味；不评判、不说教，不用「我们可以看出」这类论文腔
- 尊重原文的私人性：这是作者真实的生活记录，不是待批改的作业
- 只给值得批注的段落写，不必每段都有，整篇给 5~12 条
- 没有可说的段落就跳过，不要硬凑

输出格式（严格遵守）：只输出一个 JSON 数组，不要任何解释、前言或代码围栏：
[{"para": 1, "notes": ["批注一", "批注二"]}, {"para": 3, "notes": ["批注"]}]
其中 para 是段落编号（从 1 开始），notes 是该段的批注列表。"""

LITERARY_PROMPT = """\
你是一位既有鉴赏力又克制的中文写作教练，擅长在不改变作者声音的前提下提升文字。
我会给你一篇按段落编号的随笔，请你给「文学性提升建议」。

建议原则：
- 具体、可操作：指出问题词句 + 说明为什么 + 给出改法或改后的示例
- 关注：句子的节奏与长短、用词的准确与新鲜、意象、细节的取舍、留白、段落衔接
- 尊重作者的声音和口语感，不建议把随笔记成作文；「这里保留不动」也是一条有价值的建议
- 每条建议一两句话，引用原文词句时用「」括起来
- 只给有提升空间的段落写，整篇 5~12 条；写不出有价值建议的段落就跳过

输出格式（严格遵守）：只输出一个 JSON 数组，不要任何解释、前言或代码围栏：
[{"para": 1, "notes": ["「又往我袋子里多塞了一根葱」——「又」和「多」语义重复，留「又塞了一根葱」更干脆"]}]
其中 para 是段落编号（从 1 开始），notes 是该段的建议列表。"""

MODE_PROMPTS = {
    "interpret": INTERPRET_PROMPT,
    "literary": LITERARY_PROMPT,
}
MODE_NAMES = {mode: label for mode, label in zip(MODE_PROMPTS, MODE_LABELS)}  # "interpret" → "解读批注"
LABEL_TO_MODE = {label: mode for mode, label in zip(MODE_PROMPTS, MODE_LABELS)}


# ------------------------------------------------------------------ 段落切分与重组

def split_paragraphs(text: str) -> list[str]:
    """把 Markdown 正文按空行切成段落（图片引用行自成一段）。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "")]
    return [p for p in paras if p]


def build_user_message(paras: list[str]) -> str:
    """把编号段落拼给模型。"""
    numbered = [f"【第 {i} 段】\n{p}" for i, p in enumerate(paras, 1)]
    return "下面是我的一篇随笔，已按段落编号。请按约定输出 JSON 批注：\n\n" + "\n\n".join(
        numbered
    )


def parse_annotations(raw: str) -> dict[int, list[str]]:
    """解析模型返回的 JSON 批注，容错：剥围栏、截取最外层 []、
    整体解析失败时再逐个提取 {...} 对象。

    彻底失败返回空 dict，由上层决定是否把原文当「总评」附在文末。
    """
    text = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()

    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return {}

    items: list = []
    try:
        data = json.loads(text[start : end + 1])
        if isinstance(data, list):
            items = data
    except json.JSONDecodeError:
        # 数组里夹了非法内容：退而逐个提取形如 {...} 的对象
        items = []
        for om in re.finditer(r"\{[^{}]*\}", text):
            try:
                items.append(json.loads(om.group(0)))
            except json.JSONDecodeError:
                continue

    notes: dict[int, list[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            para = int(item.get("para"))
        except (TypeError, ValueError):
            continue
        arr = item.get("notes")
        if not isinstance(arr, list) or para < 1:
            continue
        cleaned = [str(n).strip() for n in arr if str(n or "").strip()]
        if cleaned:
            notes.setdefault(para, []).extend(cleaned)
    return notes


# ------------------------------------------------------------------ Markdown → 妙笔 HTML

_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _escape_inline(s: str) -> str:
    """转义 HTML 后应用最小的一组行内 Markdown（粗体 / 斜体 / 代码）。"""
    s = html_mod.escape(s, quote=False)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    return s


def paragraph_to_html(para: str) -> str:
    """一段 Markdown → 一个 <p>。图片引用（wonderpen:// 等）转为 <img>。

    先把图片抽成占位符再转义，避免 URL 里的字符被 HTML 转义破坏。
    """
    placeholders: list[str] = []

    def stash(m: re.Match) -> str:
        alt = html_mod.escape(m.group(1), quote=True)
        src = html_mod.escape(m.group(2), quote=True)
        placeholders.append(f'<img src="{src}" alt="{alt}">')
        return f"\x00{len(placeholders) - 1}\x00"

    body = _IMG_RE.sub(stash, para)
    body = _escape_inline(body)
    for i, tag in enumerate(placeholders):
        body = body.replace(f"\x00{i}\x00", tag)
    return f"<p>{body}</p>"


def note_to_html(note: str) -> str:
    """一条批注 → 带下划线的 <p>。"""
    return f"<p><u>💬 {_escape_inline(note)}</u></p>"


def compose_html(paras: list[str], notes: dict[int, list[str]], fallback: str = "") -> str:
    """组装整篇批注文档：每段原文后面跟它的下划线批注。

    fallback：模型没按 JSON 格式返回时，把它的原始输出作为整篇总评附在文末。
    """
    parts: list[str] = []
    for i, para in enumerate(paras, 1):
        parts.append(paragraph_to_html(para))
        for note in notes.get(i, []):
            parts.append(note_to_html(note))
    if not notes and fallback.strip():
        parts.append(note_to_html(f"（AI 这次没有按段落返回批注，以下是它的整体点评）{fallback.strip()}"))
    return "\n".join(parts)


def compose_preview(paras: list[str], notes: dict[int, list[str]], fallback: str = "") -> str:
    """预览用的纯文本形态（只读展示，实际写回妙笔的是 compose_html 的结果）。"""
    lines: list[str] = []
    for i, para in enumerate(paras, 1):
        lines.append(f"【第 {i} 段】{para}")
        for note in notes.get(i, []):
            lines.append(f"      💬 {note}")
        lines.append("")
    if not notes and fallback.strip():
        lines.append(f"      💬（整体点评）{fallback.strip()}")
    return "\n".join(lines).strip()


# ------------------------------------------------------------------ 对话模式

def chat_system_prompt(title: str, content: str, selection: str = "") -> str:
    """聊天窗的系统提示：带着文章全文（以及用户此刻在妙笔里选中的片段）。"""
    base = f"""\
你是一位懂文学的伴读者，正在陪我聊我的随笔《{title}》。文章全文附在最后。

聊天原则：
- 先读懂我写了什么，回应要落在具体的词句上，不空谈
- 我问什么答什么，可以自然延伸，但别跑题太远
- 语气像朋友聊天，真诚自然，可以有和你不同的看法
- 如果我让你改写某句某段，保持我的声音，只做必要的提升
- 回复长度适中，别把聊天写成文章

【文章全文】
{content}"""
    if selection.strip():
        base += f"\n\n【我此刻在妙笔里选中的片段】\n{selection.strip()}"
    return base
