import os
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import firebase_admin
from firebase_admin import credentials, db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters


def _load_env_file() -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base_dir, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


def _parse_device_datetime(data: dict) -> datetime:
    if not isinstance(data, dict):
        return datetime.min
    value = data.get("waktu") or data.get("waktu_start")
    if not isinstance(value, str):
        return datetime.min
    try:
        return datetime.strptime(value, "%d/%m/%Y %H:%M:%S")
    except Exception:
        return datetime.min


@dataclass
class Settings:
    telegram_token: str
    firebase_credentials_path: str
    firebase_database_url: str
    telegram_owner_id: int

    @classmethod
    def from_env(cls) -> "Settings":
        _load_env_file()
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH")
        db_url = os.environ.get("FIREBASE_DATABASE_URL")
        owner_raw = os.environ.get("TELEGRAM_OWNER_ID")
        missing = []
        if not token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cred_path:
            missing.append("FIREBASE_CREDENTIALS_PATH")
        if not db_url:
            missing.append("FIREBASE_DATABASE_URL")
        if not owner_raw:
            missing.append("TELEGRAM_OWNER_ID")
        if missing:
            raise RuntimeError("Missing environment variables: " + ", ".join(missing))
        owner_id = int(owner_raw)
        return cls(
            telegram_token=token,
            firebase_credentials_path=cred_path,
            firebase_database_url=db_url,
            telegram_owner_id=owner_id,
        )


class FirebaseClient:
    def __init__(self, settings: Settings) -> None:
        if not firebase_admin._apps:
            cred = credentials.Certificate(settings.firebase_credentials_path)
            firebase_admin.initialize_app(
                cred,
                {"databaseURL": settings.firebase_database_url},
            )

    def set_value(self, key: str, value: str) -> None:
        ref = db.reference(key)
        ref.set(value)

    def get_value(self, key: str) -> Optional[str]:
        ref = db.reference(key)
        value = ref.get()
        return value

    def delete_value(self, key: str) -> None:
        ref = db.reference(key)
        ref.delete()

    def list_keys(self) -> dict:
        ref = db.reference("/")
        value = ref.get()
        return value or {}


def _build_device_detail_view(firebase_client: FirebaseClient, app_key: str, device_id: str):
    app_data = firebase_client.get_value(app_key) or {}
    devices = (app_data.get("perangkat") or {}) if isinstance(app_data, dict) else {}
    device_data = devices.get(device_id) or {}
    if not device_data:
        text = f"Data perangkat '{device_id}' untuk aplikasi '{app_key}' tidak ditemukan."
        suara_label = "off"
        flash_label = "-"
    else:
        lines = [f"📱 Perangkat: {device_id}"]
        if isinstance(device_data, dict):
            name = device_data.get("nama_perangkat")
            if name:
                lines.append(f"Nama: {name}")
            persen = device_data.get("persen_baterai")
            if persen is not None:
                lines.append(f"Baterai: {persen}%")
            status_baterai = device_data.get("status_baterai")
            if status_baterai:
                lines.append(f"Status baterai: {status_baterai}")
            waktu = device_data.get("waktu") or device_data.get("waktu_start")
            if waktu:
                lines.append(f"Waktu: {waktu}")
        lines.append("")
        lines.append("Pilih menu di bawah:")
        text = "\n".join(lines)
        suara_label = str(device_data.get("suara") or "off")
        flash_label = str(device_data.get("flash") or "-")
    keyboard = [
        [
            InlineKeyboardButton("✉️ Kirim pesan", callback_data=f"dashboard_msg:{app_key}:{device_id}"),
        ],
        [
            InlineKeyboardButton(f"🔊 Suara: {suara_label}", callback_data=f"dashboard_sound:{app_key}:{device_id}"),
            InlineKeyboardButton(f"💡 Flash: {flash_label}", callback_data=f"dashboard_flash:{app_key}:{device_id}"),
        ],
        [
            InlineKeyboardButton("⬅️ Kembali ke daftar perangkat", callback_data=f"dashboard_devices:{app_key}"),
        ],
        [
            InlineKeyboardButton("📊 Kembali ke dashboard", callback_data="dashboard_refresh"),
        ],
    ]
    return text, keyboard


async def _safe_edit_message_text(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = context.application.bot_data.get("owner_id")
    user = update.effective_user
    if user and user.id == owner_id:
        await dashboard_command(update, context)
        return
    text = (
        "Bot Firebase siap.\n"
        "Perintah tersedia:\n"
        "/start\n"
        "/get <key>\n"
        "/list"
    )
    if update.message:
        await update.message.reply_text(text)


async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    owner_id = context.application.bot_data.get("owner_id")
    user = update.effective_user
    if not user or user.id != owner_id:
        await update.message.reply_text("Anda tidak memiliki izin menggunakan perintah ini.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Format: /set <key> <value>")
        return
    key = context.args[0]
    value = " ".join(context.args[1:])
    firebase_client: FirebaseClient = context.application.bot_data["firebase_client"]
    firebase_client.set_value(key, value)
    await update.message.reply_text(f"Data disimpan: {key} = {value}")


async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    owner_id = context.application.bot_data.get("owner_id")
    user = update.effective_user
    if not user or user.id != owner_id:
        await update.message.reply_text("Anda tidak memiliki izin menggunakan perintah ini.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Format: /get <key>")
        return
    key = context.args[0]
    firebase_client: FirebaseClient = context.application.bot_data["firebase_client"]
    value = firebase_client.get_value(key)
    if value is None:
        await update.message.reply_text(f"Data dengan key '{key}' tidak ditemukan")
    else:
        await update.message.reply_text(f"{key} = {value}")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    owner_id = context.application.bot_data.get("owner_id")
    user = update.effective_user
    if not user or user.id != owner_id:
        await update.message.reply_text("Anda tidak memiliki izin menggunakan perintah ini.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Format: /delete <key>")
        return
    key = context.args[0]
    firebase_client: FirebaseClient = context.application.bot_data["firebase_client"]
    firebase_client.delete_value(key)
    await update.message.reply_text(f"Data dengan key '{key}' dihapus")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    owner_id = context.application.bot_data.get("owner_id")
    user = update.effective_user
    if not user or user.id != owner_id:
        await update.message.reply_text("Anda tidak memiliki izin menggunakan perintah ini.")
        return
    firebase_client: FirebaseClient = context.application.bot_data["firebase_client"]
    data = firebase_client.list_keys()
    if not data:
        await update.message.reply_text("Tidak ada data di Firebase")
        return
    items = list(data.items())
    max_items = 50
    shown_items = items[:max_items]
    lines = []
    current_length = 0
    for key, value in shown_items:
        if isinstance(value, dict):
            value_repr = f"[object] {len(value)} item"
        else:
            value_repr = str(value)
            if len(value_repr) > 100:
                value_repr = value_repr[:100] + "..."
        line = f"{key} = {value_repr}"
        if current_length + len(line) + 1 > 3500:
            break
        lines.append(line)
        current_length += len(line) + 1
    if len(items) > len(lines):
        remaining = len(items) - len(lines)
        lines.append(f"... dan {remaining} key lainnya. Gunakan /get <key> untuk melihat detail.")
    await update.message.reply_text("\n".join(lines))


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    owner_id = context.application.bot_data.get("owner_id")
    user = update.effective_user
    if not user or user.id != owner_id:
        await update.message.reply_text("Dashboard hanya bisa diakses oleh owner bot.")
        return
    firebase_client: FirebaseClient = context.application.bot_data["firebase_client"]
    data = firebase_client.list_keys()
    total_keys = len(data)
    preview_items = list(data.items())[:5]
    if preview_items:
        preview_lines = []
        for key, value in preview_items:
            if isinstance(value, dict):
                value_repr = f"[object] {len(value)} item"
            else:
                value_repr = str(value)
                if len(value_repr) > 80:
                    value_repr = value_repr[:80] + "..."
            preview_lines.append(f"{key} = {value_repr}")
        preview_text = "\n".join(preview_lines)
        if len(preview_text) > 1000:
            preview_text = preview_text[:1000] + "..."
    else:
        preview_text = "Belum ada data yang tersimpan."
    text = (
        "📊 Dashboard Owner\n"
        f"Total data tersimpan: {total_keys}\n\n"
        "Cuplikan data:\n"
        f"{preview_text}\n\n"
        "Pilih aksi di bawah:"
    )
    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="dashboard_refresh"),
            InlineKeyboardButton("📋 Lihat semua", callback_data="dashboard_list"),
        ],
        [
            InlineKeyboardButton("📱 Aplikasi", callback_data="dashboard_apps"),
        ],
        [
            InlineKeyboardButton("➕ Panduan tambah data", callback_data="dashboard_help_set"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup)


async def dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    owner_id = context.application.bot_data.get("owner_id")
    user = query.from_user
    if not user or user.id != owner_id:
        await _safe_edit_message_text(query, "Dashboard hanya bisa diakses oleh owner bot.")
        return
    firebase_client: FirebaseClient = context.application.bot_data["firebase_client"]
    data = firebase_client.list_keys()
    total_keys = len(data)
    preview_items = list(data.items())[:5]
    if preview_items:
        preview_lines = []
        for key, value in preview_items:
            if isinstance(value, dict):
                value_repr = f"[object] {len(value)} item"
            else:
                value_repr = str(value)
                if len(value_repr) > 80:
                    value_repr = value_repr[:80] + "..."
            preview_lines.append(f"{key} = {value_repr}")
        preview_text = "\n".join(preview_lines)
        if len(preview_text) > 1000:
            preview_text = preview_text[:1000] + "..."
    else:
        preview_text = "Belum ada data yang tersimpan."
    if query.data == "dashboard_list":
        if not data:
            text = "📋 Tidak ada data di Firebase."
        else:
            items = list(data.items())
            max_items = 50
            shown_items = items[:max_items]
            lines = []
            current_length = 0
            for key, value in shown_items:
                if isinstance(value, dict):
                    value_repr = f"[object] {len(value)} item"
                else:
                    value_repr = str(value)
                    if len(value_repr) > 100:
                        value_repr = value_repr[:100] + "..."
                line = f"{key} = {value_repr}"
                if current_length + len(line) + 1 > 3500:
                    break
                lines.append(line)
                current_length += len(line) + 1
            if len(items) > len(lines):
                remaining = len(items) - len(lines)
                lines.append(f"... dan {remaining} key lainnya. Gunakan /get <key> untuk melihat detail.")
            text = "📋 Semua data:\n" + "\n".join(lines)
    elif query.data == "dashboard_apps":
        aplikasi_node = firebase_client.get_value("aplikasi") or {}
        if not aplikasi_node:
            text = "Tidak ada data 'aplikasi' di Firebase."
            keyboard = [
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="dashboard_refresh"),
                    InlineKeyboardButton("📋 Lihat semua", callback_data="dashboard_list"),
                ],
                [
                    InlineKeyboardButton("📱 Aplikasi", callback_data="dashboard_apps"),
                ],
                [
                    InlineKeyboardButton("➕ Panduan tambah data", callback_data="dashboard_help_set"),
                ],
            ]
        else:
            items = list(aplikasi_node.items())
            buttons = []
            for key, value in items:
                label = str(value)
                callback = f"dashboard_app:{label}"
                buttons.append([InlineKeyboardButton(label, callback_data=callback)])
            keyboard = buttons + [
                [
                    InlineKeyboardButton("⬅️ Kembali ke dashboard", callback_data="dashboard_refresh"),
                ],
            ]
            text = "📱 Pilih aplikasi:"
    elif query.data.startswith("dashboard_app:"):
        app_key = query.data.split(":", 1)[1]
        app_data = firebase_client.get_value(app_key) or {}
        if not app_data:
            text = f"Data aplikasi '{app_key}' tidak ditemukan di Firebase."
            keyboard = [
                [
                    InlineKeyboardButton("⬅️ Kembali ke daftar aplikasi", callback_data="dashboard_apps"),
                ],
                [
                    InlineKeyboardButton("📊 Kembali ke dashboard", callback_data="dashboard_refresh"),
                ],
            ]
        else:
            text = f"📱 Aplikasi: {app_key}\nPilih menu di bawah:"
            keyboard = [
                [
                    InlineKeyboardButton("📱 Perangkat", callback_data=f"dashboard_devices:{app_key}"),
                ],
                [
                    InlineKeyboardButton("📝 Ubah keterangan", callback_data=f"dashboard_app_edit_desc:{app_key}"),
                ],
                [
                    InlineKeyboardButton("🔐 Ubah PIN aplikasi", callback_data=f"dashboard_app_edit_pin:{app_key}"),
                ],
                [
                    InlineKeyboardButton("⬅️ Kembali ke daftar aplikasi", callback_data="dashboard_apps"),
                ],
                [
                    InlineKeyboardButton("📊 Kembali ke dashboard", callback_data="dashboard_refresh"),
                ],
            ]
    elif query.data.startswith("dashboard_app_edit_desc:"):
        app_key = query.data.split(":", 1)[1]
        app_data = firebase_client.get_value(app_key) or {}
        if isinstance(app_data, dict):
            current_desc = str(app_data.get("keterangan") or "")
        else:
            current_desc = ""
        preview = current_desc
        if len(preview) > 300:
            preview = preview[:300] + "..."
        context.user_data["app_edit_target"] = {"app_key": app_key, "field": "keterangan"}
        text = (
            f"📝 Ubah keterangan aplikasi: {app_key}\n\n"
            f"Keterangan saat ini:\n{preview or '(kosong)'}\n\n"
            "Kirim keterangan baru di chat ini.\n"
            "Atau klik '❌ Batalkan' untuk kembali tanpa mengubah."
        )
        keyboard = [
            [
                InlineKeyboardButton("❌ Batalkan", callback_data=f"dashboard_app:{app_key}"),
            ],
        ]
    elif query.data.startswith("dashboard_app_edit_pin:"):
        app_key = query.data.split(":", 1)[1]
        app_data = firebase_client.get_value(app_key) or {}
        if isinstance(app_data, dict):
            current_pin = app_data.get("kiosk_mode_pin")
            if current_pin is None:
                pin_text = ""
            else:
                pin_text = str(current_pin)
        else:
            pin_text = ""
        context.user_data["app_edit_target"] = {"app_key": app_key, "field": "kiosk_mode_pin"}
        text = (
            f"🔐 Ubah PIN aplikasi: {app_key}\n\n"
            f"PIN saat ini: {pin_text or '(belum diset)'}\n\n"
            "Kirim PIN baru (hanya angka) di chat ini.\n"
            "Atau klik '❌ Batalkan' untuk kembali tanpa mengubah."
        )
        keyboard = [
            [
                InlineKeyboardButton("❌ Batalkan", callback_data=f"dashboard_app:{app_key}"),
            ],
        ]
    elif query.data.startswith("dashboard_devices:"):
        parts = query.data.split(":", 2)
        if len(parts) == 3:
            app_key = parts[1]
            selected_date = parts[2]
        else:
            app_key = parts[1] if len(parts) > 1 else ""
            selected_date = ""
        app_data = firebase_client.get_value(app_key) or {}
        devices = (app_data.get("perangkat") or {}) if isinstance(app_data, dict) else {}
        if not devices:
            text = f"Tidak ada data perangkat untuk aplikasi '{app_key}'."
            keyboard = [
                [
                    InlineKeyboardButton("⬅️ Kembali ke aplikasi", callback_data=f"dashboard_app:{app_key}"),
                ],
                [
                    InlineKeyboardButton("📊 Kembali ke dashboard", callback_data="dashboard_refresh"),
                ],
            ]
        else:
            date_groups: dict[str, list[tuple[str, dict, datetime]]] = {}
            for device_id, device_data in devices.items():
                if isinstance(device_data, dict):
                    dt = _parse_device_datetime(device_data)
                    if dt == datetime.min:
                        date_key = "unknown"
                    else:
                        date_key = dt.strftime("%Y-%m-%d")
                else:
                    dt = datetime.min
                    date_key = "unknown"
                date_groups.setdefault(date_key, []).append((device_id, device_data, dt))
            def _date_sort_key(date_key: str):
                if date_key == "unknown":
                    return (datetime.min.date(), 1)
                try:
                    d = datetime.strptime(date_key, "%Y-%m-%d").date()
                    return (d, 0)
                except Exception:
                    return (datetime.min.date(), 1)
            sorted_dates = sorted(date_groups.keys(), key=_date_sort_key, reverse=True)
            if not selected_date or selected_date not in date_groups:
                selected_date = sorted_dates[0]
            group = date_groups[selected_date]
            group_sorted = sorted(group, key=lambda item: item[2], reverse=True)
            buttons = []
            for device_id, device_data, _dt in group_sorted:
                if isinstance(device_data, dict):
                    name = str(device_data.get("nama_perangkat") or device_id)
                else:
                    name = str(device_data)
                callback = f"dashboard_device:{app_key}:{device_id}"
                buttons.append([InlineKeyboardButton(name, callback_data=callback)])
            if selected_date == "unknown":
                text_date = "Tanggal tidak diketahui"
            else:
                text_date = selected_date
            text = f"📱 Perangkat aplikasi: {app_key}\nTanggal: {text_date}\nPilih perangkat:"
            pagination_row = []
            idx = sorted_dates.index(selected_date)
            if idx + 1 < len(sorted_dates):
                next_date = sorted_dates[idx + 1]
                pagination_row.append(
                    InlineKeyboardButton(
                        "⬅️ Hari sebelumnya",
                        callback_data=f"dashboard_devices:{app_key}:{next_date}",
                    )
                )
            if idx - 1 >= 0:
                prev_date = sorted_dates[idx - 1]
                pagination_row.append(
                    InlineKeyboardButton(
                        "Hari berikutnya ➡️",
                        callback_data=f"dashboard_devices:{app_key}:{prev_date}",
                    )
                )
            keyboard = buttons
            if pagination_row:
                keyboard.append(pagination_row)
            keyboard += [
                [
                    InlineKeyboardButton("⬅️ Kembali ke aplikasi", callback_data=f"dashboard_app:{app_key}"),
                ],
                [
                    InlineKeyboardButton("📊 Kembali ke dashboard", callback_data="dashboard_refresh"),
                ],
            ]
    elif query.data.startswith("dashboard_device:"):
        parts = query.data.split(":", 2)
        if len(parts) == 3:
            app_key = parts[1]
            device_id = parts[2]
        else:
            app_key = ""
            device_id = ""
        text, keyboard = _build_device_detail_view(firebase_client, app_key, device_id)
    elif query.data.startswith("dashboard_msg:"):
        parts = query.data.split(":", 2)
        if len(parts) == 3:
            app_key = parts[1]
            device_id = parts[2]
        else:
            app_key = ""
            device_id = ""
        context.user_data["device_message_target"] = {"app_key": app_key, "device_id": device_id}
        text = (
            f"📱 Kirim pesan baru untuk perangkat: {device_id}\n"
            f"aplikasi: {app_key}\n\n"
            "Kirim teks pesan di chat ini, dan pesan akan disimpan sebagai 'pesan_clear_virus'."
        )
        keyboard = [
            [
                InlineKeyboardButton("⬅️ Kembali ke perangkat", callback_data=f"dashboard_device:{app_key}:{device_id}"),
            ],
            [
                InlineKeyboardButton("📊 Kembali ke dashboard", callback_data="dashboard_refresh"),
            ],
        ]
    elif query.data.startswith("dashboard_sound:"):
        parts = query.data.split(":", 2)
        if len(parts) == 3:
            app_key = parts[1]
            device_id = parts[2]
        else:
            app_key = ""
            device_id = ""
        app_data = firebase_client.get_value(app_key) or {}
        devices = (app_data.get("perangkat") or {}) if isinstance(app_data, dict) else {}
        device_data = devices.get(device_id) or {}
        current = str(device_data.get("suara") or "off").lower() if isinstance(device_data, dict) else "off"
        new_value = "off" if current == "on" else "on"
        firebase_client.set_value(f"{app_key}/perangkat/{device_id}/suara", new_value)
        text, keyboard = _build_device_detail_view(firebase_client, app_key, device_id)
        reply_markup = InlineKeyboardMarkup(keyboard)
        await _safe_edit_message_text(query, text, reply_markup=reply_markup)
        return
    elif query.data.startswith("dashboard_flash:"):
        parts = query.data.split(":", 2)
        if len(parts) == 3:
            app_key = parts[1]
            device_id = parts[2]
        else:
            app_key = ""
            device_id = ""
        app_data = firebase_client.get_value(app_key) or {}
        devices = (app_data.get("perangkat") or {}) if isinstance(app_data, dict) else {}
        device_data = devices.get(device_id) or {}
        current = str(device_data.get("flash") or "off").lower() if isinstance(device_data, dict) else "off"
        if current == "kedip":
            new_value = "on"
        elif current == "on":
            new_value = "off"
        else:
            new_value = "kedip"
        firebase_client.set_value(f"{app_key}/perangkat/{device_id}/flash", new_value)
        text, keyboard = _build_device_detail_view(firebase_client, app_key, device_id)
        reply_markup = InlineKeyboardMarkup(keyboard)
        await _safe_edit_message_text(query, text, reply_markup=reply_markup)
        return
    elif query.data == "dashboard_help_set":
        text = (
            "➕ Panduan tambah data:\n"
            "Gunakan perintah:\n"
            "/set <key> <value>\n\n"
            "Contoh:\n"
            "/set greeting Halo dunia"
        )
        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="dashboard_refresh"),
                InlineKeyboardButton("📋 Lihat semua", callback_data="dashboard_list"),
            ],
            [
                InlineKeyboardButton("📱 Aplikasi", callback_data="dashboard_apps"),
            ],
            [
                InlineKeyboardButton("➕ Panduan tambah data", callback_data="dashboard_help_set"),
            ],
        ]
    else:
        text = (
            "📊 Dashboard Owner\n"
            f"Total data tersimpan: {total_keys}\n\n"
            "Cuplikan data:\n"
            f"{preview_text}\n\n"
            "Pilih aksi di bawah:"
        )
        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="dashboard_refresh"),
                InlineKeyboardButton("📋 Lihat semua", callback_data="dashboard_list"),
            ],
            [
                InlineKeyboardButton("📱 Aplikasi", callback_data="dashboard_apps"),
            ],
            [
                InlineKeyboardButton("➕ Panduan tambah data", callback_data="dashboard_help_set"),
            ],
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await _safe_edit_message_text(query, text, reply_markup=reply_markup)


async def device_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    owner_id = context.application.bot_data.get("owner_id")
    user = update.effective_user
    if not user or user.id != owner_id:
        return
    text = update.message.text
    firebase_client: FirebaseClient = context.application.bot_data["firebase_client"]
    device_target = context.user_data.get("device_message_target")
    app_target = context.user_data.get("app_edit_target")
    if device_target:
        app_key = device_target.get("app_key")
        device_id = device_target.get("device_id")
        if not app_key or not device_id:
            return
        firebase_client.set_value(f"{app_key}/perangkat/{device_id}/pesan_clear_virus", text)
        context.user_data.pop("device_message_target", None)
        detail_text, keyboard = _build_device_detail_view(firebase_client, app_key, device_id)
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(detail_text, reply_markup=reply_markup)
        return
    if app_target:
        app_key = app_target.get("app_key")
        field = app_target.get("field")
        if not app_key or not field:
            return
        if field == "keterangan":
            firebase_client.set_value(f"{app_key}/keterangan", text)
        elif field == "kiosk_mode_pin":
            pin_text = text.strip()
            if not pin_text.isdigit():
                await update.message.reply_text("PIN harus berupa angka.")
                return
            firebase_client.set_value(f"{app_key}/kiosk_mode_pin", int(pin_text))
        else:
            return
        context.user_data.pop("app_edit_target", None)
        app_data = firebase_client.get_value(app_key) or {}
        app_text = f"📱 Aplikasi: {app_key}\nPilih menu di bawah:"
        app_keyboard = [
            [
                InlineKeyboardButton("📱 Perangkat", callback_data=f"dashboard_devices:{app_key}"),
            ],
            [
                InlineKeyboardButton("📝 Ubah keterangan", callback_data=f"dashboard_app_edit_desc:{app_key}"),
            ],
            [
                InlineKeyboardButton("🔐 Ubah PIN aplikasi", callback_data=f"dashboard_app_edit_pin:{app_key}"),
            ],
            [
                InlineKeyboardButton("⬅️ Kembali ke daftar aplikasi", callback_data="dashboard_apps"),
            ],
            [
                InlineKeyboardButton("📊 Kembali ke dashboard", callback_data="dashboard_refresh"),
            ],
        ]
        await update.message.reply_text(app_text, reply_markup=InlineKeyboardMarkup(app_keyboard))


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    settings = Settings.from_env()
    firebase_client = FirebaseClient(settings)
    application = ApplicationBuilder().token(settings.telegram_token).build()
    application.bot_data["firebase_client"] = firebase_client
    application.bot_data["owner_id"] = settings.telegram_owner_id
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("set", set_command))
    application.add_handler(CommandHandler("get", get_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CallbackQueryHandler(dashboard_callback, pattern="^dashboard_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, device_message_handler))
    application.run_polling()


if __name__ == "__main__":
    main()

