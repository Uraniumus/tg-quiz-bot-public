from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import ssl
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import telebot
from dotenv import load_dotenv
from telebot import types

from game_registration import (
    add_waitlist,
    get_game_basic,
    get_latest_poll_for_game,
    get_upcoming_game_with_counts,
    list_game_registrants,
    list_upcoming_games_with_counts,
    register_player,
    record_poll_answer,
    remove_waitlist_entry,
    unregister_player,
)
from product_features import build_personal_stats, previous_month_key
from helpers import format_game_datetime
from ui import build_calendar_file, build_calendar_url, build_invite_text


MAX_REQUEST_BYTES = 16_384
MAX_INIT_DATA_AGE_SECONDS = 3_600


SHARE_PAGE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Приглашение на квиз</title>
  <script src="https://telegram.org/js/telegram-web-app.js?59"></script>
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #111); }
    main { width: min(88vw, 420px); text-align: center; }
    .icon { font-size: 52px; margin-bottom: 12px; }
    h1 { font-size: 22px; margin: 0 0 10px; }
    p { color: var(--tg-theme-hint-color, #777); line-height: 1.45; }
    button { width: 100%; border: 0; border-radius: 12px; padding: 14px 18px; font-size: 17px; font-weight: 600; color: var(--tg-theme-button-text-color, #fff); background: var(--tg-theme-button-color, #2481cc); }
    button[hidden] { display: none; }
  </style>
</head>
<body>
  <main>
    <div class="icon">💌</div>
    <h1>Готовим приглашение</h1>
    <p id="status">Сейчас откроется выбор получателя…</p>
    <button id="share" hidden>Выбрать, кому отправить</button>
  </main>
  <script>
    const gameId = __GAME_ID__;
    const tg = window.Telegram && window.Telegram.WebApp;
    const statusNode = document.getElementById('status');
    const shareButton = document.getElementById('share');
    let preparedId = null;

    function showManual(text) {
      statusNode.textContent = text;
      shareButton.hidden = false;
    }

    function sharePrepared() {
      if (!tg || !preparedId || typeof tg.shareMessage !== 'function') {
        showManual('Обновите Telegram до последней версии и попробуйте снова.');
        return;
      }
      tg.shareMessage(preparedId, function (sent) {
        if (sent) tg.close();
        else showManual('Нажмите кнопку, чтобы выбрать получателя.');
      });
    }

    shareButton.addEventListener('click', sharePrepared);

    async function start() {
      if (!tg || !tg.initData) {
        showManual('Откройте эту страницу кнопкой внутри Telegram.');
        return;
      }
      tg.ready();
      tg.expand();
      try {
        const response = await fetch('/prepare', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({game_id: gameId, init_data: tg.initData})
        });
        const data = await response.json();
        if (!response.ok || !data.prepared_id) throw new Error(data.error || 'prepare failed');
        preparedId = data.prepared_id;
        sharePrepared();
      } catch (error) {
        showManual('Не удалось подготовить приглашение. Закройте окно и попробуйте ещё раз.');
      }
    }

    start();
  </script>
</body>
</html>
"""


APP_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Квизы</title><script src="https://telegram.org/js/telegram-web-app.js?63"></script>
<style>:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--tg-theme-text-color,#111);background:var(--tg-theme-bg-color,#fff)}*{box-sizing:border-box}body{margin:0;padding:14px 13px calc(30px + env(safe-area-inset-bottom))}h1{font-size:27px;margin:4px 2px 14px}.tabs{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;position:sticky;top:0;z-index:3;background:var(--tg-theme-bg-color,#fff);padding:4px 0 10px}.tabs button,.action,.choice{border:0;border-radius:11px;padding:11px 10px;font-size:14px;background:var(--tg-theme-secondary-bg-color,#eee);color:inherit}.tabs button.active,.action.primary,.choice.active{background:var(--tg-theme-button-color,#2481cc);color:var(--tg-theme-button-text-color,#fff)}.card{background:var(--tg-theme-secondary-bg-color,#f2f2f2);border-radius:18px;padding:15px;margin:10px 0}.title{font-size:18px;font-weight:750;margin-bottom:8px}.muted{color:var(--tg-theme-hint-color,#777);font-size:14px;line-height:1.45}.status{margin:10px 0;line-height:1.45}.actions{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:12px}.actions .wide{grid-column:1/-1}.choices{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin:12px 0}.participants{margin:10px 0 0;padding:10px 12px;border-radius:12px;background:var(--tg-theme-bg-color,#fff);font-size:14px;line-height:1.6}.desc{white-space:pre-line;margin-top:9px}.empty{color:var(--tg-theme-hint-color,#777);padding:30px 8px;text-align:center}#error{color:#d33;margin:10px 2px}.loading{text-align:center;padding:28px;color:var(--tg-theme-hint-color,#777)}dialog{width:min(92vw,430px);border:0;border-radius:18px;padding:18px;background:var(--tg-theme-bg-color,#fff);color:inherit}dialog::backdrop{background:#0008}.sheet button{display:block;width:100%;margin:8px 0;border:0;border-radius:12px;padding:13px;background:var(--tg-theme-secondary-bg-color,#eee);color:inherit;font-size:15px}.close{float:right;border:0;background:none;color:var(--tg-theme-link-color,#2481cc);font-size:15px}.stats{white-space:pre-line;line-height:1.65}.spinner{opacity:.55;pointer-events:none}</style></head><body><h1>Квизы</h1><div class="tabs"><button data-tab="upcoming" class="active">🎯 Все</button><button data-tab="mine">✅ Мои</button><button data-tab="stats">📊 Стата</button></div><div id="error"></div><main id="content" class="loading">Загружаем…</main><dialog id="sheet"><button class="close" onclick="sheet.close()">Готово</button><div id="sheetBody" class="sheet"></div></dialog><script>
const tg=window.Telegram&&window.Telegram.WebApp,content=document.getElementById('content'),error=document.getElementById('error'),sheet=document.getElementById('sheet'),sheetBody=document.getElementById('sheetBody');let state=null,tab='upcoming',busy=false;if(tg){tg.ready();tg.expand();tg.BackButton&&tg.BackButton.onClick(()=>sheet.open?sheet.close():tg.close())}function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}function js(v){return String(v??'')}function openUrl(u){tg&&tg.openLink?tg.openLink(u):location.href=u}function name(p){return p.username?'@'+p.username:(p.full_name||'участник')}function participants(g){return g.participants.length?g.participants.map((p,i)=>`${i+1}. ${esc(name(p))}`).join('<br>'):'Пока никто не отметился'}function card(g){let free=Math.max(g.capacity-g.registrations,0),mine=g.is_registered?'yes':g.is_waitlisted?'wait':g.is_maybe?'maybe':g.is_not_going?'no':'none',loc=[g.restaurant_name,g.restaurant_address].filter(Boolean).join(', ');return `<section class="card" id="game-${g.id}"><div class="title">${esc(g.title)}</div><div class="muted">🗓 ${esc(g.game_date||'Дата уточняется')}<br>📍 ${esc(loc||'Место уточняется')}</div>${g.description?`<div class="muted desc">${esc(g.description)}</div>`:''}<div class="status">👥 ${g.registrations} из ${g.capacity}${free?` · свободно ${free}`:' · мест нет'}${g.waitlist_count?`<br>⏳ В резерве: ${g.waitlist_count}`:''}</div><div class="choices"><button class="choice ${mine==='yes'?'active':''}" onclick="act(${g.id},'signup')">✅ Иду</button><button class="choice ${mine==='maybe'?'active':''}" onclick="act(${g.id},'maybe')">🤔 Может</button><button class="choice ${mine==='no'?'active':''}" onclick="act(${g.id},'cancel')">❌ Не иду</button></div><details><summary>Кто идёт (${g.registrations})</summary><div class="participants">${participants(g)}</div></details><div class="actions"><button class="action" onclick="invite(${g.id})">💌 Позвать</button><button class="action" onclick="routes(${g.id})">📍 Маршрут</button><button class="action" onclick="cal(${g.id})">📅 Календарь</button>${g.poll_url?`<button class="action" onclick="tg.openTelegramLink('${js(g.poll_url)}')">💬 В беседу</button>`:''}</div></section>`}function findGame(id){return state.games.find(g=>g.id===id)}function show(html){sheetBody.innerHTML=html;sheet.showModal();tg&&tg.BackButton&&tg.BackButton.show()}sheet.addEventListener('close',()=>tg&&tg.BackButton&&tg.BackButton.hide());function routes(id){let g=findGame(id),q=encodeURIComponent([g.restaurant_name,g.restaurant_address].filter(Boolean).join(', '));show(`<h3>Куда едем?</h3><button onclick="openUrl('https://yandex.ru/maps/?text=${q}')">🟡 Яндекс Карты</button><button onclick="openUrl('https://2gis.ru/spb/search/${q}')">🟢 2ГИС</button><button onclick="openUrl('https://maps.apple.com/?q=${q}')">⚪️ Apple Карты</button>`)}function cal(id){let g=findGame(id);show(`<h3>Добавить в календарь</h3><button onclick="openUrl('${js(g.calendar_url)}')">🟦 Google Calendar</button><button onclick="downloadCal(${id})">🍎 Apple / Outlook / другой</button>`)}function downloadCal(id){let u=location.origin+findGame(id).calendar_file;if(tg&&tg.downloadFile)tg.downloadFile({url:u,file_name:'quiz-'+id+'.ics'});else openUrl(u)}async function invite(id){try{setBusy(true);let d=await api('/prepare',{game_id:id});if(tg&&tg.shareMessage)tg.shareMessage(d.prepared_id);else throw Error('Обнови Telegram')}catch(e){error.textContent=e.message}finally{setBusy(false)}}function setBusy(v){busy=v;document.body.classList.toggle('spinner',v)}function render(){document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));if(!state)return;if(tab==='stats'){content.className='';content.innerHTML=`<div class="choices"><button class="choice active" onclick="stats('current',this)">Месяц</button><button class="choice" onclick="stats('previous',this)">Прошлый</button><button class="choice" onclick="stats('all',this)">Всё время</button></div><section class="card stats" id="statsText">${esc(state.stats.current)}</section>`;return}let games=tab==='mine'?state.games.filter(g=>g.is_registered||g.is_waitlisted||g.is_maybe):state.games;content.className='';content.innerHTML=games.length?games.map(card).join(''):'<div class="empty">Здесь пока нет игр.</div>'}async function stats(period,btn){document.querySelectorAll('.choices .choice').forEach(b=>b.classList.remove('active'));btn.classList.add('active');document.getElementById('statsText').textContent=state.stats[period]}async function api(path,body){let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,init_data:tg.initData})}),d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка');return d}async function act(id,a){if(busy)return;try{setBusy(true);error.textContent='';state=await api('/api/action',{game_id:id,action:a});render();requestAnimationFrame(()=>document.getElementById('game-'+id)?.scrollIntoView({block:'center'}));tg&&tg.HapticFeedback&&tg.HapticFeedback.notificationOccurred('success')}catch(e){error.textContent=e.message}finally{setBusy(false)}}document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;render()});async function start(){if(!tg||!tg.initData){error.textContent='Откройте приложение внутри Telegram';return}try{state=await api('/api/bootstrap',{});render();let p=tg.initDataUnsafe&&tg.initDataUnsafe.start_param;if(p&&p.startsWith('game_'))setTimeout(()=>document.getElementById('game-'+p.slice(5))?.scrollIntoView({behavior:'smooth'}),100)}catch(e){error.textContent='Не удалось загрузить игры';content.className=''}}start();</script></body></html>"""


def validate_webapp_init_data(
    init_data: str,
    bot_token: str,
    now: int | None = None,
    max_age_seconds: int = MAX_INIT_DATA_AGE_SECONDS,
) -> dict:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise ValueError("missing hash")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise ValueError("invalid hash")

    current_time = int(now if now is not None else time.time())
    try:
        auth_date = int(values["auth_date"])
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid auth date") from exc
    if auth_date > current_time + 60 or current_time - auth_date > max_age_seconds:
        raise ValueError("expired init data")
    try:
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid user") from exc
    return {"user_id": user_id, "user": user}


def create_share_server(
    bot: telebot.TeleBot,
    db_path: str,
    bot_token: str,
    bot_username: str,
) -> type[BaseHTTPRequestHandler]:
    def serialize_game(game: dict, user_id: int) -> dict:
        result = dict(game)
        result["game_date"] = format_game_datetime(game.get("game_date"))
        result["calendar_url"] = build_calendar_url(game)
        result["calendar_file"] = f"/calendar?game_id={int(game['id'])}"
        result["participants"] = list_game_registrants(
            db_path, int(game["id"]), include_cancelled=False
        )
        poll = get_latest_poll_for_game(db_path, int(game["id"]))
        result["poll_url"] = None
        if poll and str(poll["chat_id"]).startswith("-100"):
            result["poll_url"] = (
                f"https://t.me/c/{str(poll['chat_id'])[4:]}/{poll['message_id']}"
            )
        result["is_not_going"] = False
        if poll:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT option_id FROM poll_answers WHERE poll_id = ? AND user_id = ?",
                    (poll["poll_id"], user_id),
                ).fetchone()
            result["is_not_going"] = bool(row and int(row[0]) == 1)
        return result

    def user_state(user_id: int, username: str | None = None) -> dict:
        games = [
            serialize_game(game, user_id)
            for game in list_upcoming_games_with_counts(db_path, user_id)
        ]
        previous = previous_month_key(datetime.now(ZoneInfo("Europe/Moscow")).date())
        return {
            "games": games,
            "stats": {
                "current": build_personal_stats(db_path, user_id, username, "current"),
                "previous": build_personal_stats(db_path, user_id, username, previous),
                "all": build_personal_stats(db_path, user_id, username, "all"),
            },
            "bot_username": bot_username,
        }

    class ShareRequestHandler(BaseHTTPRequestHandler):
        server_version = "QPShare/1.0"

        def log_message(self, message_format: str, *args) -> None:
            logging.info("share-app %s", message_format % args)

        def send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self.send_json(200, {"ok": True})
                return
            if parsed.path == "/app":
                body = APP_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; style-src 'self' 'unsafe-inline'; connect-src 'self';")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/calendar":
                try:
                    game_id = int(parse_qs(parsed.query).get("game_id", [""])[0])
                    game = get_game_basic(db_path, game_id)
                    calendar = build_calendar_file(game) if game else None
                    if not calendar:
                        raise ValueError
                except (ValueError, TypeError):
                    self.send_error(404, "calendar not found")
                    return
                body, filename = calendar
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Access-Control-Allow-Origin", "https://web.telegram.org")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=300")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path != "/share":
                self.send_error(404)
                return
            try:
                game_id = int(parse_qs(parsed.query).get("game_id", [""])[0])
                if game_id < 1:
                    raise ValueError
            except ValueError:
                self.send_error(400, "invalid game")
                return
            body = SHARE_PAGE.replace("__GAME_ID__", str(game_id)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path not in {"/prepare", "/api/bootstrap", "/api/action"}:
                self.send_error(404)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(content_length))
                verified = validate_webapp_init_data(
                    str(payload["init_data"]), bot_token
                )
                user_id = verified["user_id"]
                if path == "/api/bootstrap":
                    self.send_json(200, user_state(user_id, verified["user"].get("username")))
                    return
                if path == "/api/action":
                    game_id = int(payload["game_id"])
                    action = str(payload["action"])
                    game = get_upcoming_game_with_counts(db_path, user_id, game_id)
                    if not game:
                        self.send_json(404, {"error": "game not found"})
                        return
                    username = verified["user"].get("username")
                    full_name = " ".join(
                        str(verified["user"].get(key, "")).strip()
                        for key in ("first_name", "last_name")
                        if verified["user"].get(key)
                    )
                    poll = get_latest_poll_for_game(db_path, game_id)
                    if action == "signup":
                        if int(game.get("registrations") or 0) >= int(game.get("capacity") or 9) and not game.get("is_registered"):
                            add_waitlist(db_path, game_id, user_id, username, full_name)
                        else:
                            register_player(db_path, game_id, user_id, username, full_name)
                        if poll:
                            record_poll_answer(db_path, poll["poll_id"], user_id, 0, username, full_name)
                    elif action == "cancel":
                        unregister_player(db_path, game_id, user_id)
                        remove_waitlist_entry(db_path, game_id, user_id)
                        if poll:
                            record_poll_answer(db_path, poll["poll_id"], user_id, 1, username, full_name)
                    elif action == "maybe":
                        unregister_player(db_path, game_id, user_id)
                        remove_waitlist_entry(db_path, game_id, user_id)
                        if poll:
                            record_poll_answer(db_path, poll["poll_id"], user_id, 2, username, full_name)
                    elif action == "waitlist":
                        add_waitlist(db_path, game_id, user_id, username, full_name)
                    else:
                        self.send_json(400, {"error": "unknown action"})
                        return
                    self.send_json(200, user_state(user_id, username))
                    return
                game_id = int(payload["game_id"])
                game = get_upcoming_game_with_counts(db_path, user_id, game_id)
                if not game:
                    self.send_json(404, {"error": "game not found"})
                    return
                poll_url = f"https://t.me/{bot_username}?startapp=game_{game_id}"
                message_content = types.InputTextMessageContent(
                    build_invite_text(game, poll_url, "Открыть игру и отметиться"),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                result = types.InlineQueryResultArticle(
                    id=f"game-{game_id}-{uuid.uuid4().hex[:12]}",
                    title=f"Приглашение: {game.get('title') or 'Квиз'}",
                    input_message_content=message_content,
                )
                prepared = bot.save_prepared_inline_message(
                    user_id,
                    result,
                    allow_user_chats=True,
                    allow_group_chats=True,
                    allow_channel_chats=True,
                )
                self.send_json(200, {"prepared_id": prepared.id})
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                self.send_json(400, {"error": "invalid request"})
            except Exception:
                logging.exception("Failed to prepare Telegram share message")
                self.send_json(500, {"error": "internal error"})

    return ShareRequestHandler


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    load_dotenv()
    bot_token = os.environ["BOT_TOKEN"]
    db_path = os.getenv("DB_PATH", "quiz_bot.sqlite3")
    host = os.getenv("SHARE_APP_HOST", "0.0.0.0")
    port = int(os.getenv("SHARE_APP_PORT", "8443"))
    certificate_path = os.environ["SHARE_APP_CERT"]
    key_path = os.environ["SHARE_APP_KEY"]
    bot = telebot.TeleBot(bot_token)
    bot_username = (bot.get_me().username or "QPHelperBot").lstrip("@")
    handler = create_share_server(bot, db_path, bot_token, bot_username)
    server = ThreadingHTTPServer((host, port), handler)
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(certificate_path, key_path)
    server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    logging.info("Quiz share app listening on %s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
