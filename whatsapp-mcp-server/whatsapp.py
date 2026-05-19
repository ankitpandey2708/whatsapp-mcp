import sqlite3
from contextlib import contextmanager
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import sys
import re
import audio

# ── Timestamp parsing ────────────────────────────────────────────────────────

def parse_timestamp(ts: str) -> datetime:
    """Parse timestamps from messages.db which come in two formats:
    - '2026-05-19 23:01:55 +0530 IST'  (Go time.String() format)
    - '2026-05-14 23:07:51+05:30'       (ISO 8601)
    """
    if not ts:
        return datetime.min
    ts_clean = re.sub(r'\s+[A-Z]{2,5}$', '', ts.strip())
    ts_clean = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', ts_clean)
    try:
        return datetime.fromisoformat(ts_clean)
    except ValueError:
        ts_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', ts_clean).strip()
        return datetime.fromisoformat(ts_clean)

# ── Constants ─────────────────────────────────────────────────────────────────

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSAPP_STORE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'whatsapp.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"

# ── Shared DB helpers ─────────────────────────────────────────────────────────

@contextmanager
def open_db(path: str):
    """Context manager that opens a SQLite connection and closes it on exit."""
    conn = sqlite3.connect(path)
    try:
        yield conn
    finally:
        conn.close()

def make_placeholders(items: List) -> str:
    """Return a comma-separated '?,?,?' string for SQL IN clauses."""
    return ','.join('?' * len(items))

# Correlated subquery that reliably picks the single latest message per chat.
# Avoids the timestamp-equality JOIN that breaks when two messages share a timestamp.
# Requires the chats table to be aliased as 'c' in the outer query.
_LAST_MSG_JOIN = """
    LEFT JOIN messages m ON m.rowid = (
        SELECT rowid FROM messages WHERE chat_jid = c.jid ORDER BY timestamp DESC LIMIT 1
    )
"""

# ── Contact name helper ───────────────────────────────────────────────────────

def get_contact_name_from_jid(jid: str, conn) -> Optional[str]:
    """Look up the best display name for a phone JID in whatsmeow_contacts."""
    try:
        row = conn.execute(
            "SELECT COALESCE(NULLIF(full_name,''), NULLIF(push_name,''), NULLIF(first_name,'')) "
            "FROM whatsmeow_contacts WHERE their_jid = ? LIMIT 1",
            (jid,)
        ).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None

# ── LID ↔ phone resolution ───────────────────────────────────────────────────

def lid_to_phone(lid: str) -> Optional[str]:
    try:
        lid_num = lid.split('@')[0] if '@' in lid else lid
        with open_db(WHATSAPP_STORE_DB_PATH) as conn:
            row = conn.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (lid_num,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def phone_to_lid(phone: str) -> Optional[str]:
    try:
        phone_num = phone.split('@')[0] if '@' in phone else phone
        with open_db(WHATSAPP_STORE_DB_PATH) as conn:
            row = conn.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ?", (phone_num,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None

def resolve_jid(jid: str) -> str:
    """Resolve a LID JID to its phone-based JID; pass through everything else."""
    if jid.endswith('@lid'):
        phone = lid_to_phone(jid)
        if phone:
            return f"{phone}@s.whatsapp.net"
    return jid

def get_chat_jids_for_phone(phone: str) -> List[str]:
    phone_num = phone.split('@')[0] if '@' in phone else phone
    jids = [f"{phone_num}@s.whatsapp.net"]
    lid = phone_to_lid(phone_num)
    if lid:
        jids.append(f"{lid}@lid")
    return jids

def expand_chat_jid(chat_jid: str) -> List[str]:
    if chat_jid.endswith('@g.us'):
        return [chat_jid]
    if chat_jid.endswith('@lid'):
        phone = lid_to_phone(chat_jid)
        if phone:
            return [chat_jid, f"{phone}@s.whatsapp.net"]
        return [chat_jid]
    phone_num = chat_jid.split('@')[0]
    return get_chat_jids_for_phone(phone_num)

def resolve_chat_name(jid: str, stored_name: Optional[str], store_conn=None) -> str:
    """If stored name is just a raw LID number, look up the real contact name."""
    if stored_name and not stored_name.isdigit():
        return stored_name
    phone = lid_to_phone(jid) if jid.endswith('@lid') else jid.split('@')[0]
    if phone:
        try:
            owns_conn = store_conn is None
            conn = store_conn if store_conn is not None else sqlite3.connect(WHATSAPP_STORE_DB_PATH)
            try:
                name = get_contact_name_from_jid(f"{phone}@s.whatsapp.net", conn)
                if name:
                    return name
            finally:
                if owns_conn:
                    conn.close()
        except Exception:
            pass
    return stored_name or jid

# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None
    sender_name: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

# ── Row parsers ───────────────────────────────────────────────────────────────

def parse_message_row(msg, media_type_col: int = 7) -> Message:
    """Parse a standard 8-column message SELECT row into a Message object."""
    return Message(
        timestamp=parse_timestamp(msg[0]),
        sender=msg[1],
        chat_name=msg[2],
        content=msg[3],
        is_from_me=msg[4],
        chat_jid=msg[5],
        id=msg[6],
        media_type=msg[media_type_col],
    )

def parse_chat_row(chat_data, store_conn=None) -> Chat:
    """Parse a standard 6-column chat SELECT row into a Chat object."""
    return Chat(
        jid=chat_data[0],
        name=resolve_chat_name(chat_data[0], chat_data[1], store_conn),
        last_message_time=parse_timestamp(chat_data[2]) if chat_data[2] else None,
        last_message=chat_data[3],
        last_sender=chat_data[4],
        last_is_from_me=chat_data[5],
    )

# ── API helpers ───────────────────────────────────────────────────────────────

def _validate_media_send(recipient: str, media_path: str) -> Optional[Tuple[bool, str]]:
    """Return an error tuple if inputs are invalid, else None."""
    if not recipient:
        return False, "Recipient must be provided"
    if not media_path:
        return False, "Media path must be provided"
    if not os.path.isfile(media_path):
        return False, f"Media file not found: {media_path}"
    return None

def _call_api(url: str, payload: dict) -> Tuple[Optional[dict], str]:
    """POST to the bridge REST API. Returns (parsed_json, error_message)."""
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            return response.json(), ""
        return None, f"Error: HTTP {response.status_code} - {response.text}"
    except requests.RequestException as e:
        return None, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return None, f"Error parsing response: {response.text}"
    except Exception as e:
        return None, f"Unexpected error: {str(e)}"

def _post_to_api(url: str, payload: dict) -> Tuple[bool, str]:
    """POST to the bridge REST API and return (success, message)."""
    result, err = _call_api(url, payload)
    if result is None:
        return False, err
    return result.get("success", False), result.get("message", "Unknown response")

# ── Sender name resolution ────────────────────────────────────────────────────

def get_sender_name(sender_jid: str) -> str:
    """Resolve a sender (stored as raw user part) to a display name."""
    try:
        raw = sender_jid.split('@')[0] if '@' in sender_jid else sender_jid

        candidates = [f"{raw}@s.whatsapp.net", f"{raw}@lid"]
        phone_from_lid = lid_to_phone(raw)
        if phone_from_lid:
            candidates.extend([f"{phone_from_lid}@s.whatsapp.net", f"{phone_from_lid}@lid"])
        lid_from_phone = phone_to_lid(raw)
        if lid_from_phone:
            candidates.extend([f"{lid_from_phone}@lid", f"{lid_from_phone}@s.whatsapp.net"])

        phone_candidates = [f"{raw}@s.whatsapp.net"]
        if phone_from_lid:
            phone_candidates.append(f"{phone_from_lid}@s.whatsapp.net")

        # Fix #9: open both DBs together instead of two sequential connections
        with open_db(MESSAGES_DB_PATH) as msg_conn, open_db(WHATSAPP_STORE_DB_PATH) as store_conn:
            for jid in candidates:
                row = msg_conn.execute("SELECT name FROM chats WHERE jid = ? LIMIT 1", (jid,)).fetchone()
                if row and row[0] and not row[0].isdigit():
                    return row[0]
            for jid in phone_candidates:
                name = get_contact_name_from_jid(jid, store_conn)
                if name:
                    return name

        return raw
    except Exception as e:
        print(f"get_sender_name error: {e}", file=sys.stderr)
        return sender_jid

# ── Formatting ────────────────────────────────────────────────────────────────

def format_message(message: Message, show_chat_info: bool = True) -> str:
    output = ""
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "

    content_prefix = ""
    if message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "

    try:
        if message.is_from_me:
            sender_label = "Me"
        else:
            phone = message.sender.split('@')[0] if '@' in message.sender else message.sender
            resolved = message.sender_name or get_sender_name(message.sender)
            sender_label = f"{resolved} ({phone})" if resolved != phone else phone
        output += f"From: {sender_label}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}", file=sys.stderr)
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> str:
    if not messages:
        return "No messages to display."
    return "".join(format_message(m, show_chat_info) for m in messages)

# ── Public query functions ────────────────────────────────────────────────────

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> str:  # Fix #1: was annotated List[Message] but always returned str
    """Get messages matching the specified criteria with optional context."""
    try:
        with open_db(MESSAGES_DB_PATH) as conn:
            query_parts = [
                "SELECT messages.timestamp, messages.sender, chats.name, messages.content, "
                "messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages",
                "JOIN chats ON messages.chat_jid = chats.jid",
            ]
            where_clauses = []
            params = []

            if after:
                try:
                    after = datetime.fromisoformat(after)
                except ValueError:
                    raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
                where_clauses.append("messages.timestamp > ?")
                params.append(after)

            if before:
                try:
                    before = datetime.fromisoformat(before)
                except ValueError:
                    raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
                where_clauses.append("messages.timestamp < ?")
                params.append(before)

            if sender_phone_number:
                phone_num = sender_phone_number.split('@')[0] if '@' in sender_phone_number else sender_phone_number
                sender_variants = [phone_num]
                lid = phone_to_lid(phone_num)
                if lid:
                    sender_variants.append(lid)
                where_clauses.append(f"messages.sender IN ({make_placeholders(sender_variants)})")
                params.extend(sender_variants)

            if chat_jid:
                jid_variants = expand_chat_jid(chat_jid)
                where_clauses.append(f"messages.chat_jid IN ({make_placeholders(jid_variants)})")
                params.extend(jid_variants)

            if query:
                where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
                params.append(f"%{query}%")

            if where_clauses:
                query_parts.append("WHERE " + " AND ".join(where_clauses))

            query_parts.append("ORDER BY messages.timestamp DESC")
            query_parts.append("LIMIT ? OFFSET ?")
            params.extend([limit, page * limit])

            cursor = conn.execute(" ".join(query_parts), tuple(params))
            result = [parse_message_row(row) for row in cursor.fetchall()]

            # Fix #8: batch all context queries inside the same connection (was N+1 open_db calls)
            if include_context and result:
                messages_with_context = []
                for msg in result:
                    before_rows = conn.execute("""
                        SELECT messages.timestamp, messages.sender, chats.name, messages.content,
                               messages.is_from_me, chats.jid, messages.id, messages.media_type
                        FROM messages JOIN chats ON messages.chat_jid = chats.jid
                        WHERE messages.chat_jid = ? AND messages.timestamp < ?
                        ORDER BY messages.timestamp DESC LIMIT ?
                    """, (msg.chat_jid, msg.timestamp, context_before)).fetchall()
                    after_rows = conn.execute("""
                        SELECT messages.timestamp, messages.sender, chats.name, messages.content,
                               messages.is_from_me, chats.jid, messages.id, messages.media_type
                        FROM messages JOIN chats ON messages.chat_jid = chats.jid
                        WHERE messages.chat_jid = ? AND messages.timestamp > ?
                        ORDER BY messages.timestamp ASC LIMIT ?
                    """, (msg.chat_jid, msg.timestamp, context_after)).fetchall()
                    messages_with_context.extend(parse_message_row(r) for r in reversed(before_rows))
                    messages_with_context.append(msg)
                    messages_with_context.extend(parse_message_row(r) for r in after_rows)
                return format_messages_list(messages_with_context, show_chat_info=True)

            return format_messages_list(result, show_chat_info=True)

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return ""  # Fix #1: was returning [] (wrong type)


def get_message_context(message_id: str, before: int = 5, after: int = 5) -> Optional[MessageContext]:
    """Get context around a specific message."""
    try:
        with open_db(MESSAGES_DB_PATH) as conn:
            row = conn.execute("""
                SELECT messages.timestamp, messages.sender, chats.name, messages.content,
                       messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
                FROM messages
                JOIN chats ON messages.chat_jid = chats.jid
                WHERE messages.id = ?
            """, (message_id,)).fetchone()

            if not row:
                raise ValueError(f"Message with ID {message_id} not found")

            target = parse_message_row(row, media_type_col=8)
            chat_jid_val, ts_val = row[7], row[0]

            before_rows = conn.execute("""
                SELECT messages.timestamp, messages.sender, chats.name, messages.content,
                       messages.is_from_me, chats.jid, messages.id, messages.media_type
                FROM messages JOIN chats ON messages.chat_jid = chats.jid
                WHERE messages.chat_jid = ? AND messages.timestamp < ?
                ORDER BY messages.timestamp DESC LIMIT ?
            """, (chat_jid_val, ts_val, before)).fetchall()

            after_rows = conn.execute("""
                SELECT messages.timestamp, messages.sender, chats.name, messages.content,
                       messages.is_from_me, chats.jid, messages.id, messages.media_type
                FROM messages JOIN chats ON messages.chat_jid = chats.jid
                WHERE messages.chat_jid = ? AND messages.timestamp > ?
                ORDER BY messages.timestamp ASC LIMIT ?
            """, (chat_jid_val, ts_val, after)).fetchall()

        return MessageContext(
            message=target,
            before=[parse_message_row(r) for r in before_rows],
            after=[parse_message_row(r) for r in after_rows],
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None  # Fix #7: was re-raising; now consistent with every other function


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        with open_db(MESSAGES_DB_PATH) as conn, open_db(WHATSAPP_STORE_DB_PATH) as store_conn:
            query_parts = ["""
                SELECT c.jid, c.name, c.last_message_time,
                       m.content as last_message,
                       m.sender as last_sender,
                       m.is_from_me as last_is_from_me
                FROM chats c
            """]
            if include_last_message:
                query_parts.append(_LAST_MSG_JOIN)  # Fix #5: correlated subquery; was timestamp-equality JOIN
            where_clauses, params = [], []
            if query:
                where_clauses.append("(LOWER(c.name) LIKE LOWER(?) OR c.jid LIKE ?)")
                params.extend([f"%{query}%", f"%{query}%"])
            if where_clauses:
                query_parts.append("WHERE " + " AND ".join(where_clauses))

            order_by = "c.last_message_time DESC" if sort_by == "last_active" else "c.name"
            query_parts.append(f"ORDER BY {order_by} LIMIT ? OFFSET ?")
            params.extend([limit, page * limit])

            rows = conn.execute(" ".join(query_parts), tuple(params)).fetchall()
            return [parse_chat_row(r, store_conn) for r in rows]

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return []


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    pattern = f'%{query}%'
    seen: dict = {}

    try:
        with open_db(WHATSAPP_STORE_DB_PATH) as conn:
            rows = conn.execute("""
                SELECT DISTINCT their_jid,
                    COALESCE(NULLIF(full_name,''), NULLIF(push_name,''), NULLIF(first_name,''), their_jid) as name
                FROM whatsmeow_contacts
                WHERE their_jid NOT LIKE '%@g.us'
                  AND (LOWER(full_name) LIKE LOWER(?) OR LOWER(push_name) LIKE LOWER(?)
                    OR LOWER(first_name) LIKE LOWER(?) OR their_jid LIKE ?)
                ORDER BY name LIMIT 50
            """, (pattern, pattern, pattern, pattern)).fetchall()
            for jid, name in rows:
                # Fix #4: resolve LID JIDs to real phone numbers instead of exposing raw LID
                phone = (lid_to_phone(jid) or jid.split('@')[0]) if jid.endswith('@lid') else jid.split('@')[0]
                seen[jid] = Contact(phone_number=phone, name=name, jid=jid)
    except sqlite3.Error as e:
        print(f"whatsapp.db search error: {e}", file=sys.stderr)

    try:
        with open_db(MESSAGES_DB_PATH) as conn:
            rows = conn.execute("""
                SELECT DISTINCT jid, name FROM chats
                WHERE jid NOT LIKE '%@g.us'
                  AND (LOWER(name) LIKE LOWER(?) OR jid LIKE ?)
                ORDER BY name, jid LIMIT 50
            """, (pattern, pattern)).fetchall()
            for jid, name in rows:
                phone = (lid_to_phone(jid) or jid.split('@')[0]) if jid.endswith('@lid') else jid.split('@')[0]
                if jid not in seen:
                    seen[jid] = Contact(phone_number=phone, name=name, jid=jid)
                elif name and not seen[jid].name:
                    seen[jid] = Contact(phone_number=phone, name=name, jid=jid)
    except sqlite3.Error as e:
        print(f"messages.db search error: {e}", file=sys.stderr)

    return sorted(seen.values(), key=lambda c: (c.name or '').lower())


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact."""
    try:
        # Fix #3: was get_chat_jids_for_phone which assumes phone input and breaks on LID JIDs
        jids = expand_chat_jid(jid)
        ph = make_placeholders(jids)
        with open_db(MESSAGES_DB_PATH) as conn, open_db(WHATSAPP_STORE_DB_PATH) as store_conn:
            rows = conn.execute(f"""
                SELECT c.jid, c.name, c.last_message_time,
                       m.content as last_message, m.sender as last_sender,
                       m.is_from_me as last_is_from_me
                FROM chats c
                {_LAST_MSG_JOIN}
                WHERE c.jid IN (
                    SELECT DISTINCT chat_jid FROM messages WHERE sender IN ({ph})
                    UNION
                    SELECT jid FROM chats WHERE jid IN ({ph})
                )
                ORDER BY c.last_message_time DESC
                LIMIT ? OFFSET ?
            """, jids + jids + [limit, page * limit]).fetchall()
            return [parse_chat_row(r, store_conn) for r in rows]

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return []


def get_last_interaction(jid: str) -> Optional[str]:
    """Get most recent message involving the contact."""
    try:
        jids = get_chat_jids_for_phone(jid)
        ph = make_placeholders(jids)
        with open_db(MESSAGES_DB_PATH) as conn:
            row = conn.execute(f"""
                SELECT m.timestamp, m.sender, c.name, m.content,
                       m.is_from_me, c.jid, m.id, m.media_type
                FROM messages m
                JOIN chats c ON m.chat_jid = c.jid
                WHERE m.sender IN ({ph}) OR c.jid IN ({ph})
                ORDER BY m.timestamp DESC LIMIT 1
            """, jids + jids).fetchone()

        if not row:
            return None
        return format_message(parse_message_row(row))

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        jid_variants = expand_chat_jid(chat_jid)
        ph = make_placeholders(jid_variants)
        query = """
            SELECT c.jid, c.name, c.last_message_time,
                   m.content as last_message, m.sender as last_sender,
                   m.is_from_me as last_is_from_me
            FROM chats c
        """
        if include_last_message:
            query += _LAST_MSG_JOIN  # Fix #5: correlated subquery; was timestamp-equality JOIN
        query += f" WHERE c.jid IN ({ph})"

        with open_db(MESSAGES_DB_PATH) as conn:
            row = conn.execute(query, jid_variants).fetchone()

        if not row:
            return None
        return parse_chat_row(row)

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        jids = get_chat_jids_for_phone(sender_phone_number)
        ph = make_placeholders(jids)
        with open_db(MESSAGES_DB_PATH) as conn:
            row = conn.execute(f"""
                SELECT c.jid, c.name, c.last_message_time,
                       m.content as last_message, m.sender as last_sender,
                       m.is_from_me as last_is_from_me
                FROM chats c
                {_LAST_MSG_JOIN}
                WHERE c.jid IN ({ph})
                LIMIT 1
            """, jids).fetchone()

        if not row:
            return None
        return parse_chat_row(row)

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None


# ── Send / receive ────────────────────────────────────────────────────────────

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    if not recipient:
        return False, "Recipient must be provided"
    # Fix #2: resolve LID JIDs before sending — Go bridge doesn't handle @lid addresses
    return _post_to_api(f"{WHATSAPP_API_BASE_URL}/send", {"recipient": resolve_jid(recipient), "message": message})


def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    if err := _validate_media_send(recipient, media_path):
        return err
    return _post_to_api(f"{WHATSAPP_API_BASE_URL}/send", {"recipient": resolve_jid(recipient), "media_path": media_path})


def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    if err := _validate_media_send(recipient, media_path):
        return err
    if not media_path.endswith(".ogg"):
        try:
            media_path = audio.convert_to_opus_ogg_temp(media_path)
        except Exception as e:
            return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
    return _post_to_api(f"{WHATSAPP_API_BASE_URL}/send", {"recipient": resolve_jid(recipient), "media_path": media_path})


def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path."""
    # Fix #6: reuse _call_api instead of duplicating the HTTP error-handling ladder
    result, err = _call_api(f"{WHATSAPP_API_BASE_URL}/download", {"message_id": message_id, "chat_jid": chat_jid})
    if result is None:
        print(err, file=sys.stderr)
        return None
    if result.get("success"):
        path = result.get("path")
        print(f"Media downloaded successfully: {path}", file=sys.stderr)
        return path
    print(f"Download failed: {result.get('message', 'Unknown error')}", file=sys.stderr)
    return None
