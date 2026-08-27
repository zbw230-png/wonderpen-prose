// 妙笔散文日记 · Web 版
// Cloudflare Worker：静态页面 + /api/polish 代理智谱 GLM（API Key 只存服务端 Secret）
// 部署：cd web && npx wrangler deploy

const GLM_API_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
const DEFAULT_MODEL = 'glm-5.2'
const MAX_INPUT_CHARS = 30000

// 与桌面版 polisher.py 的 SYSTEM_PROMPT 保持一致（忠实润色风格）
const SYSTEM_PROMPT = `你是一位中文散文写作高手，擅长把随手记下的生活随笔，梳理成质感温润的散文日记。

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
- 不要输出任何解释、前言、后语，你的回复就是正文本身`

export default {
  async fetch(request, env) {
    const url = new URL(request.url)

    if (url.pathname === '/api/polish') {
      return handlePolish(request, env)
    }
    if (url.pathname === '/api/config') {
      // 告诉前端是否需要访问码（不泄露码本身）
      return json({ needsPasscode: !!env.PASSCODE }, 200)
    }

    return env.ASSETS.fetch(request)
  },
}

async function handlePolish(request, env) {
  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders() })
  }
  if (request.method !== 'POST') {
    return json({ error: '请用 POST 请求' }, 405)
  }

  let body
  try {
    body = await request.json()
  } catch {
    return json({ error: '请求格式错误' }, 400)
  }

  const text = String(body.text || '').trim()
  if (!text) {
    return json({ error: '请先粘贴随笔内容' }, 400)
  }
  if (text.length > MAX_INPUT_CHARS) {
    return json({ error: `随笔太长了（${text.length} 字，上限 ${MAX_INPUT_CHARS} 字），请分次梳理` }, 400)
  }

  // 访问码（可选）：防止公开网址被人盗刷智谱额度
  if (env.PASSCODE && String(body.passcode || '') !== env.PASSCODE) {
    return json({ error: '访问码不对，请核对后重试' }, 401)
  }

  const apiKey = env.GLM_API_KEY
  if (!apiKey) {
    return json({ error: '服务端还没配置智谱 API Key（GLM_API_KEY）' }, 500)
  }

  const glmResp = await fetch(GLM_API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model: env.GLM_MODEL || DEFAULT_MODEL,
      messages: [
        { role: 'system', content: SYSTEM_PROMPT },
        { role: 'user', content: `下面是我写的一篇随笔，请把它梳理成一篇散文日记：\n\n${text}` },
      ],
      stream: true,
      temperature: 0.7,
      max_tokens: 8192,
    }),
  })

  if (!glmResp.ok) {
    const errText = await glmResp.text()
    let msg = `智谱 API 出错（${glmResp.status}）`
    if (glmResp.status === 401) msg = '智谱 API Key 无效，请到 Cloudflare 后台检查 GLM_API_KEY'
    if (glmResp.status === 429) msg = '智谱 API 调用太频繁或额度不足，稍等一下再试'
    return json({ error: msg, detail: errText.slice(0, 500) }, 502)
  }

  // 把智谱的 SSE 流原样透传给前端（text/event-stream）
  return new Response(glmResp.body, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      ...corsHeaders(),
    },
  })
}

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      ...corsHeaders(),
    },
  })
}
