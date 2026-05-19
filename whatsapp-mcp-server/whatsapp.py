import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import sys
import re
import audio

def parse_timestamp(ts: str) -> datetime:
    """Parse timestamps from messages.db which come in two formats:
    - '2026-05-19 23:01:55 +0530 IST'  (Go time.String() format)
    - '2026-05-14 23:07:51+05:30'       (ISO 8601)
    """
    if not ts:
        return datetime.min
    # Strip trailing timezone name like ' IST', ' UTC', ' EST' etc.
    ts_clean = re.sub(r'\s+[A-Z]{2,5}$', '', ts.strip())
    # Normalize '+0530' → '+05:30'
    ts_clean = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', ts_clean)
    try:
        return datetime.fromisoformat(ts_clean)
    except ValueError:
        # Last resort: strip timezone entirely
        ts_clean = re.sub(r'[+-]\d{2}:\d{2}$', '', ts_clean).strip()
        return datetime.fromisoformat(ts_clean)

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSAPP_STORE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'whatsapp.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"

def lid_to_phone(lid: str) -> Optional[str]:
    """Resolve a LID (or LID JID like 123@lid) to a phone number."""
    try:
        lid_num = lid.split('@')[0] if '@' in lid else lid
        conn = sqlite3.connect(WHATSAPP_STORE_DB_PATH)
        row = conn.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (lid_num,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def phone_to_lid(phone: str) -> Optional[str]:
    """Resolve a phone number to a LID."""
    try:
        phone_num = phone.split('@')[0] if '@' in phone else phone
        conn = sqlite3.connect(WHATSAPP_STORE_DB_PATH)
        row = conn.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ?", (phone_num,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def resolve_jid(jid: str) -> str:
    """Return phone@s.whatsapp.net for a LID JID, otherwise return as-is."""
    if jid.endswith('@lid'):
        phone = lid_to_phone(jid)
        if phone:
            return f"{phone}@s.whatsapp.net"
    return jid

def get_chat_jids_for_phone(phone: str) -> List[str]:
    """Return all JID variants for a phone number (e.g. @s.whatsapp.net and @lid)."""
    phone_num = phone.split('@')[0] if '@' in phone else phone
    jids = [f"{phone_num}@s.whatsapp.net"]
    lid = phone_to_lid(phone_num)
    if lid:
        jids.append(f"{lid}@lid")
    return jids

def expand_chat_jid(chat_jid: str) -> List[str]:
    """Given any JID variant, return all known variants for that chat."""
    if chat_jid.endswith('@g.us'):
        return [chat_jid]
    if chat_jid.endswith('@lid'):
        phone = lid_to_phone(chat_jid)
        if phone:
            return [chat_jid, f"{phone}@s.whatsapp.net"]
        return [chat_jid]
    # phone JID
    phone_num = chat_jid.split('@')[0]
    return get_chat_jids_for_phone(phone_num)

def resolve_chat_name(jid: str, stored_name: Optional[str], store_conn=None) -> str:
    """If stored name is just a raw LID number, look up the real contact name."""
    if stored_name and not stored_name.isdigit():
        return stored_name
    # Numeric-only name means it was stored as LID — resolve it
    phone = lid_to_phone(jid) if jid.endswith('@lid') else jid.split('@')[0]
    if phone:
        try:
            owns_conn = store_conn is None
            conn = store_conn if store_conn is not None else sqlite3.connect(WHATSAPP_STORE_DB_PATH)
            try:
                row = conn.execute(
                    "SELECT COALESCE(NULLIF(full_name,''), NULLIF(push_name,''), NULLIF(first_name,'')) FROM whatsmeow_contacts WHERE their_jid = ? LIMIT 1",
                    (f"{phone}@s.whatsapp.net",)
                ).fetchone()
                if row and row[0]:
                    return row[0]
            finally:
                if owns_conn:
                    conn.close()
        except Exception:
            pass
    return stored_name or jid

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
        """Determine if chat is a group based on JID pattern."""
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

def get_sender_name(sender_jid: str) -> str:
    """Resolve a sender (stored as raw user part, e.g. '107962676867112' or '919113044636') to a display name."""
    try:
        raw = sender_jid.split('@')[0] if '@' in sender_jid else sender_jid

        # Build all JID candidates to check
        # Senders are stored as raw numbers (no @suffix), could be phone or LID
        candidates_chats = [f"{raw}@s.whatsapp.net", f"{raw}@lid"]

        # Try resolving raw as a LID → get phone
        phone_from_lid = lid_to_phone(raw)
        if phone_from_lid:
            candidates_chats.append(f"{phone_from_lid}@s.whatsapp.net")
            candidates_chats.append(f"{phone_from_lid}@lid")

        # Try resolving raw as a phone → get LID
        lid_from_phone = phone_to_lid(raw)
        if lid_from_phone:
            candidates_chats.append(f"{lid_from_phone}@lid")
            candidates_chats.append(f"{lid_from_phone}@s.whatsapp.net")

        # Check messages.db chats table
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        for jid in candidates_chats:
            row = conn.execute("SELECT name FROM chats WHERE jid = ? LIMIT 1", (jid,)).fetchone()
            if row and row[0] and not row[0].isdigit():
                conn.close()
                return row[0]
        conn.close()

        # Check whatsapp.db contacts
        phone_candidates = [f"{raw}@s.whatsapp.net"]
        if phone_from_lid:
            phone_candidates.append(f"{phone_from_lid}@s.whatsapp.net")
        conn2 = sqlite3.connect(WHATSAPP_STORE_DB_PATH)
        try:
            for jid in phone_candidates:
                row = conn2.execute(
                    "SELECT COALESCE(NULLIF(full_name,''), NULLIF(push_name,''), NULLIF(first_name,'')) FROM whatsmeow_contacts WHERE their_jid = ? LIMIT 1",
                    (jid,)
                ).fetchone()
                if row and row[0]:
                    return row[0]
            return raw
        finally:
            conn2.close()

    except Exception as e:
        print(f"get_sender_name error: {e}", file=sys.stderr)
        return sender_jid

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
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

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

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
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
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
            placeholders = ','.join('?' * len(sender_variants))
            where_clauses.append(f"messages.sender IN ({placeholders})")
            params.extend(sender_variants)
            
        if chat_jid:
            jid_variants = expand_chat_jid(chat_jid)
            placeholders = ','.join('?' * len(jid_variants))
            where_clauses.append(f"messages.chat_jid IN ({placeholders})")
            params.extend(jid_variants)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=parse_timestamp(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            )
            result.append(message)
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = Message(
            timestamp=parse_timestamp(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=parse_timestamp(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=parse_timestamp(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["""
            SELECT 
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                AND chats.last_message_time = messages.timestamp
            """)
            
        where_clauses = []
        params = []
        
        if query:
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        store_conn = sqlite3.connect(WHATSAPP_STORE_DB_PATH)
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=resolve_chat_name(chat_data[0], chat_data[1], store_conn),
                last_message_time=parse_timestamp(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return []
    finally:
        if 'store_conn' in locals():
            store_conn.close()
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    search_pattern = f'%{query}%'
    seen_jids = {}  # jid -> Contact, for dedup

    # 1. Query whatsapp.db (full contact list from WhatsApp)
    try:
        conn = sqlite3.connect(WHATSAPP_STORE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT their_jid,
                COALESCE(NULLIF(full_name,''), NULLIF(push_name,''), NULLIF(first_name,''), their_jid) as name
            FROM whatsmeow_contacts
            WHERE their_jid NOT LIKE '%@g.us'
              AND (LOWER(full_name) LIKE LOWER(?)
                OR LOWER(push_name) LIKE LOWER(?)
                OR LOWER(first_name) LIKE LOWER(?)
                OR their_jid LIKE ?)
            ORDER BY name
            LIMIT 50
        """, (search_pattern, search_pattern, search_pattern, search_pattern))
        for jid, name in cursor.fetchall():
            seen_jids[jid] = Contact(phone_number=jid.split('@')[0], name=name, jid=jid)
    except sqlite3.Error as e:
        print(f"whatsapp.db search error: {e}", file=sys.stderr)
    finally:
        if 'conn' in locals():
            conn.close()

    # 2. Query messages.db (chats with synced history) — fills gaps and updates names
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT jid, name
            FROM chats
            WHERE jid NOT LIKE '%@g.us'
              AND (LOWER(name) LIKE LOWER(?) OR jid LIKE ?)
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern))
        for jid, name in cursor.fetchall():
            if jid not in seen_jids:
                seen_jids[jid] = Contact(phone_number=jid.split('@')[0], name=name, jid=jid)
            elif name and not seen_jids[jid].name:
                seen_jids[jid] = Contact(phone_number=jid.split('@')[0], name=name, jid=jid)
    except sqlite3.Error as e:
        print(f"messages.db search error: {e}", file=sys.stderr)
    finally:
        if 'conn' in locals():
            conn.close()

    return sorted(seen_jids.values(), key=lambda c: (c.name or '').lower())


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        jids = get_chat_jids_for_phone(jid)
        placeholders = ','.join('?' * len(jids))
        sender_placeholders = placeholders

        cursor.execute(f"""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender IN ({sender_placeholders}) OR c.jid IN ({placeholders})
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, jids + jids + [limit, page * limit])
        
        chats = cursor.fetchall()
        
        store_conn = sqlite3.connect(WHATSAPP_STORE_DB_PATH)
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=resolve_chat_name(chat_data[0], chat_data[1], store_conn),
                last_message_time=parse_timestamp(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return []
    finally:
        if 'store_conn' in locals():
            store_conn.close()
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        jids = get_chat_jids_for_phone(jid)
        placeholders = ','.join('?' * len(jids))

        cursor.execute(f"""
            SELECT
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender IN ({placeholders}) OR c.jid IN ({placeholders})
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, jids + jids)
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=parse_timestamp(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        jid_variants = expand_chat_jid(chat_jid)
        placeholders = ','.join('?' * len(jid_variants))
        query += f" WHERE c.jid IN ({placeholders})"

        cursor.execute(query, jid_variants)
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=resolve_chat_name(chat_data[0], chat_data[1]),
            last_message_time=parse_timestamp(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        jids = get_chat_jids_for_phone(sender_phone_number)
        placeholders = ','.join('?' * len(jids))

        cursor.execute(f"""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE c.jid IN ({placeholders})
            LIMIT 1
        """, jids)

        chat_data = cursor.fetchone()
        if not chat_data:
            return None

        return Chat(
            jid=chat_data[0],
            name=resolve_chat_name(chat_data[0], chat_data[1]),
            last_message_time=parse_timestamp(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}", file=sys.stderr)
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}", file=sys.stderr)
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}", file=sys.stderr)
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}", file=sys.stderr)
        return None
