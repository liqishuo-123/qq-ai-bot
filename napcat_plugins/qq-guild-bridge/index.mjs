const TEXT_ELEMENT = 1;
const CHAT_TYPE_GUILD = 4;

function ok(data, echo) {
  return {
    status: "ok",
    retcode: 0,
    data: data ?? null,
    message: "",
    wording: "",
    echo: echo ?? "",
  };
}

function fail(error, echo) {
  const message = error instanceof Error ? error.message : String(error);
  return {
    status: "failed",
    retcode: 200,
    data: null,
    message,
    wording: message,
    echo: echo ?? "",
  };
}

function requiredId(value, name) {
  const text = String(value ?? "").trim();
  if (!/^\d{1,30}$/.test(text)) throw new Error(`${name} 格式不对`);
  return text;
}

function textElement(content) {
  return {
    elementType: TEXT_ELEMENT,
    elementId: "",
    textElement: {
      content,
      atType: 0,
      atUid: "",
      atTinyId: "",
      atNtUid: "",
    },
  };
}

function messagePreview(message) {
  const elements = Array.isArray(message?.elements) ? message.elements : [];
  const parts = [];
  for (const element of elements) {
    if (element?.textElement?.content) parts.push(String(element.textElement.content));
    else if (element?.faceElement?.faceText) parts.push(String(element.faceElement.faceText));
    else if (element?.picElement) parts.push("[图片]");
  }
  return parts.join("").slice(0, 500);
}

export async function plugin_init(ctx) {
  const actions = ctx.actions;
  if (!actions || typeof actions.set !== "function") {
    throw new Error("NapCat actions registry is unavailable");
  }

  const msgApi = ctx.core?.apis?.MsgApi;
  const hasSend = Boolean(msgApi && typeof msgApi.sendMsg === "function");

  function requireMsgApi() {
    if (!msgApi || typeof msgApi.sendMsg !== "function") {
      throw new Error("QQ NT MsgApi.sendMsg is unavailable");
    }
    return msgApi;
  }

  async function status() {
    return {
      available: hasSend,
      account: String(ctx.core?.selfInfo?.uin ?? ""),
      note: "同账号频道桥接已加载；频道列表仍需手工 guild_id/channel_id。",
    };
  }

  async function send(params) {
    const guildId = requiredId(params.guild_id, "guild_id");
    const channelId = requiredId(params.channel_id, "channel_id");
    const content = String(params.content ?? "").trim();
    if (!content) throw new Error("频道内容为空");
    if (content.length > 2000) throw new Error("频道内容超过 2000 字");
    const peer = {
      chatType: CHAT_TYPE_GUILD,
      peerUid: channelId,
    };
    const result = await requireMsgApi().sendMsg(peer, [textElement(content)], 15000);
    return {
      ok: true,
      guild_id: guildId,
      channel_id: channelId,
      message_id: String(result?.msgId ?? result?.messageId ?? ""),
    };
  }

  async function history(params) {
    const channelId = requiredId(params.channel_id, "channel_id");
    const limit = Math.max(1, Math.min(Number(params.limit) || 20, 50));
    const peer = {
      chatType: CHAT_TYPE_GUILD,
      peerUid: channelId,
    };
    const api = requireMsgApi();
    if (typeof api.getAioFirstViewLatestMsgs !== "function") {
      throw new Error("当前 QQ NT 不支持频道历史读取");
    }
    const result = await api.getAioFirstViewLatestMsgs(peer, limit);
    const messages = Array.isArray(result?.msgList)
      ? result.msgList
      : Array.isArray(result?.messages)
        ? result.messages
        : [];
    return {
      ok: true,
      messages: messages.map((message) => ({
        message_id: String(message?.msgId ?? ""),
        sender_id: String(message?.senderUin ?? ""),
        content: messagePreview(message),
        created_at: String(message?.msgTime ?? ""),
      })),
    };
  }

  async function list(params) {
    const guildId = String(params.guild_id ?? "").trim();
    const channelId = String(params.channel_id ?? "").trim();
    return {
      ok: true,
      guilds: guildId ? [{ id: guildId, name: "配置的频道" }] : [],
      channels: channelId
        ? [{ id: channelId, name: "配置的频道", guild_id: guildId }]
        : [],
      note: "NapCat 未暴露频道列表，只能返回控制台配置的频道。",
    };
  }

  function wrap(fn) {
    return {
      async websocketHandle(params, echo) {
        try {
          return ok(await fn(params ?? {}), echo);
        } catch (error) {
          return fail(error, echo);
        }
      },
      async handle(params) {
        try {
          return ok(await fn(params ?? {}), "");
        } catch (error) {
          return fail(error, "");
        }
      },
    };
  }

  const handlers = {
    nc_guild_status: wrap(status),
    nc_guild_send: wrap(send),
    nc_guild_history: wrap(history),
    nc_guild_list: wrap(list),
  };
  for (const [name, handler] of Object.entries(handlers)) actions.set(name, handler);
  ctx.logger?.info?.("QQ guild bridge loaded");
}

export async function plugin_cleanup(ctx) {
  for (const name of ["nc_guild_status", "nc_guild_send", "nc_guild_history", "nc_guild_list"]) {
    ctx.actions?.delete?.(name);
  }
}