"""把随笔梳理成散文日记：提示词与编排。

风格定位是「忠实润色」——保留原意、细节与语气，只梳理结构、
理顺时间线、修正口误、提升文气，不添油加醋。
"""

import datetime

SYSTEM_PROMPT = """\
你是一位中文散文写作高手，擅长把随手记下的生活随笔，梳理成质感温润的散文日记。

你的工作原则（忠实润色）：

一、忠实
- 保留原文中所有真实的人物、事件、地点、细节与感悟
- 绝不虚构原文没有发生过的事，不添加新的情节、对话或感想
- 原作者的语气和性格要留住：ta 怎么看世界，就怎么写

二、梳理
- 理顺时间线与叙述顺序，合并重复出现的思绪
- 原文多为随手记或语音输入，会有错别字、口误、不成句的片段：
  请按上下文理解本意，修正明显的笔误，把破碎的句子接圆
- 与主题无关的纯粹赘词可以删去，但具体的细节（天气、温度、食物、
  动作、名字）尽量保留——散文的生命在细节里

三、质感
- 以第一人称写散文日记，像一个人在晚上安静地回顾这一天
- 句子有呼吸感，长短相间，节奏自然
- 情感克制而真诚，避免堆砌形容词，避免空洞的抒情和升华
- 不要刻意拔高立意，写到哪算哪，留有余味

四、格式约定
- 直接输出日记正文（Markdown），不要写标题
- 不要小标题、编号、列表、表格，用自然段落书写，段与段之间空一行
- 原文中的图片引用（形如 ![](wonderpen://assets/xxx) 的整行）
  必须原样保留其中的地址，放在文中情节对应的位置，一个都不能丢
- 不要输出任何解释、前言、后语，你的回复就是正文本身
"""


def build_user_message(docs: list[dict]) -> str:
    """把选中的随笔拼成给模型的输入。

    docs: [{"id", "title", "content", "created_at_ms"(可无)}]，已按时间排序。
    单篇直接给原文；多篇按时间顺序分段，说明它们是同一时期的随笔。
    """
    parts = []
    for i, doc in enumerate(docs, 1):
        header = f"【随笔 {i}：{doc.get('title', '无标题')}】"
        ts = doc.get("created_at_ms")
        if ts:
            t = datetime.datetime.fromtimestamp(ts / 1000)
            header += f"（写于 {t:%Y年%m月%d日}）"
        parts.append(f"{header}\n\n{doc['content'].strip()}")

    if len(docs) == 1:
        lead = "下面是我的一篇随笔，请把它梳理成一篇散文日记：\n\n"
    else:
        lead = (
            f"下面是我同一时期写的 {len(docs)} 篇随笔，请按时间顺序把它们"
            "融成一篇完整的散文日记（不要分篇输出，要写成连贯的一篇）：\n\n"
        )
    return lead + "\n\n".join(parts)


def suggest_title(docs: list[dict]) -> str:
    """为梳理结果起一个默认的新文档标题，用户可在界面里改。"""
    if len(docs) == 1:
        base = docs[0].get("rendered_title") or docs[0].get("title") or "随笔"
        return f"{base} · 散文"
    ts = None
    for doc in docs:
        if doc.get("created_at_ms"):
            ts = doc["created_at_ms"]
    if ts:
        t = datetime.datetime.fromtimestamp(ts / 1000)
        return f"散文 · {t:%m月%d日}"
    return "散文 · 随笔合集"


def sort_docs(docs: list[dict]) -> list[dict]:
    """按创建时间升序排列（无时间的排前面），保证日记的时间顺序。"""

    def key(doc):
        return doc.get("created_at_ms") or 0

    return sorted(docs, key=key)
