'use strict';

const fs = require('fs');
const path = require('path');

const target = path.join(
  process.cwd(),
  'node_modules',
  'whatsapp-web.js',
  'src',
  'util',
  'Injected',
  'Utils.js',
);

let source = fs.readFileSync(target, 'utf8');

function replaceOnce(before, after, label) {
  const first = source.indexOf(before);
  if (first === -1) {
    if (source.includes(after)) {
      console.log(`whatsapp-web.js patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply whatsapp-web.js patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Patch signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied whatsapp-web.js patch: ${label}`);
}

replaceOnce(
  `.Msg.get(newMsgKey._serialized);`,
  `.Msg.get(window.WWebJS.getMsgKeyId(newMsgKey));`,
  'sendMessage message-key compatibility',
);

replaceOnce(
  `return window.require('WAWebCollections').Msg.get(msg.id._serialized);`,
  `return window\n            .require('WAWebCollections')\n            .Msg.get(window.WWebJS.getMsgKeyId(msg.id));`,
  'editMessage message-key compatibility',
);

replaceOnce(
  `        delete msg.pendingAckUpdate;\n\n        return msg;`,
  `        // WhatsApp Web 2.3000.104xxx renamed message-key _serialized to $1.\n        // Restore the public shape expected by whatsapp-web.js and SHVYA.\n        if (typeof msg.id === 'object' && msg.id._serialized == null) {\n            const serializedId = window.WWebJS.getMsgKeyId(msg.id);\n            if (serializedId) {\n                msg.id = Object.assign({}, msg.id, {\n                    _serialized: serializedId,\n                });\n            }\n        }\n\n        delete msg.pendingAckUpdate;\n\n        return msg;`,
  'serialized message id restoration',
);

replaceOnce(
  `    window.WWebJS.getChats = async () => {\n        const chats = window.require('WAWebCollections').Chat.getModelsArray();\n        const chatPromises = chats.map((chat) =>\n            window.WWebJS.getChatModel(chat),\n        );\n        return await Promise.all(chatPromises);\n    };`,
  `    /**\n     * Read a serialized message key across old and current WhatsApp Web builds.\n     * Current 2.3000.104xxx builds expose $1 instead of _serialized.\n     */\n    window.WWebJS.getMsgKeyId = (key) =>\n        key?._serialized ?? key?.$1 ?? undefined;\n\n    window.WWebJS.getChats = async () => {\n        const chats = window.require('WAWebCollections').Chat.getModelsArray();\n\n        // Promise.all is fail-fast. A single @lid chat that cannot refresh\n        // IndexedDB group metadata used to make the whole inbox disappear.\n        const results = [];\n        for (const chat of chats) {\n            try {\n                const model = await window.WWebJS.getChatModel(chat);\n                if (model) results.push(model);\n            } catch {\n                // Skip only the broken chat instead of failing every chat.\n            }\n        }\n        return results;\n    };`,
  'LID-safe getChats and message-key helper',
);

replaceOnce(
  `            await groupMetadata.update(chatWid);`,
  `            try {\n                await groupMetadata.update(chatWid);\n            } catch {\n                // LID-based chat ids may not exist in this IndexedDB lookup.\n                // Keep the chat and omit only group metadata.\n                model.groupMetadata = null;\n            }`,
  'LID group metadata isolation',
);

replaceOnce(
  `        model.lastMessage = null;\n        if (model.msgs && model.msgs.length) {\n            const lastMessage = chat.lastReceivedKey\n                ? window\n                      .require('WAWebCollections')\n                      .Msg.get(chat.lastReceivedKey._serialized) ||\n                  (\n                      await window\n                          .require('WAWebCollections')\n                          .Msg.getMessagesById([\n                              chat.lastReceivedKey._serialized,\n                          ])\n                  )?.messages?.[0]\n                : null;`,
  `        model.lastMessage = null;\n        if (model.msgs && model.msgs.length) {\n            const lastReceivedKeyId = window.WWebJS.getMsgKeyId(\n                chat.lastReceivedKey,\n            );\n            const lastMessage = lastReceivedKeyId\n                ? window\n                      .require('WAWebCollections')\n                      .Msg.get(lastReceivedKeyId) ||\n                  (\n                      await window\n                          .require('WAWebCollections')\n                          .Msg.getMessagesById([lastReceivedKeyId])\n                  )?.messages?.[0]\n                : null;`,
  'last-message key compatibility',
);

fs.writeFileSync(target, source);

if (!source.includes('window.WWebJS.getMsgKeyId')) {
  throw new Error('whatsapp-web.js compatibility patch verification failed');
}

console.log('whatsapp-web.js 1.34.7 compatibility patch complete.');
