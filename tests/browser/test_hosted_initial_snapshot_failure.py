"""Real JS race regression with an isolated DOM and no external traffic."""

from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def test_live_message_survives_failed_initial_history_request():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed locally")
            raise
        page = browser.new_page()
        try:
            page.set_content('''<style>.hidden{display:none}</style>
              <input name="csrfmiddlewaretoken" value="test">
              <div id="hosted-chat-app" data-account-id="account" data-account-status="connected"
                   data-data-url="/data/" data-send-url="/send/">
              <div id="conversation-list"></div><input id="chat-search"><div id="search-meta"></div>
              <div id="thread-header"><span id="thread-name"></span><span id="thread-key"></span>
              <span id="thread-avatar"></span><span id="thread-intent-score"></span></div>
              <div id="thread-scroll"></div><div id="thread-empty"></div>
              <form id="chat-form"><textarea name="body"></textarea><input id="chat-key-input">
              <button type="submit">Send</button></form>
              <div id="hosted-live-status"><span data-live-label></span></div></div>''')
            page.evaluate('''() => {
              history.replaceState=()=>{};
              window.testControl={};
              window.WebSocket=class {
                static OPEN=1; static CONNECTING=0;
                constructor(){this.readyState=1;testControl.socket=this;setTimeout(()=>this.onopen?.(),0)}
                close(){this.readyState=3;this.onclose?.({code:1011})}
              };
              window.fetch=async url=>{
                const chat=new URL(url,'https://test.invalid/').searchParams.get('chat')||'';
                if(chat) return new Promise((resolve,reject)=>{testControl.rejectHistory=reject});
                return {ok:true,status:200,json:async()=>({selected_chat:'',thread:[],
                  conversations:[{key:'+919999999999',name:'Test contact',unread:0}],
                  account_status:'connected',has_more:false})};
              };
            }''')
            for name in ("hosted_chat_live_state.js", "hosted_whatsapp_chat.js"):
                page.add_script_tag(content=(ROOT / "static/js" / name).read_text())
            page.locator('[data-chat-key="+919999999999"]').click()
            page.wait_for_function("typeof testControl.rejectHistory === 'function'")
            page.evaluate('''() => testControl.socket.onmessage({data:JSON.stringify({
              kind:'message',account_id:'account',chat_key:'+919999999999',operation:'upsert',
              message_id:'live-message',updated_at:'2026-10-03T00:00:01Z',
              message:{id:'live-message',body:'Already received live',direction:'inbound',
                status:'received',message_type:'text',created_at:'2026-10-03T00:00:01Z'}
            })})''')
            bubble = page.locator('[data-message-id="live-message"]')
            expect(bubble).to_contain_text("Already received live", timeout=2000)
            page.evaluate("testControl.rejectHistory(new Error('initial history request failed'))")
            page.wait_for_timeout(100)
            expect(bubble).to_contain_text("Already received live")
            expect(page.locator(".thread-error")).to_have_count(0)
        finally:
            browser.close()
