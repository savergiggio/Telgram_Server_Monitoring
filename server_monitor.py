import os
import time
import json
import psutil
import re
import ipaddress
import subprocess
import socket
from datetime import datetime
from threading import Thread
from flask import Flask, render_template, request, redirect
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters

# Rimossa importazione delle funzioni di navigazione directory
# Queste funzioni sono ora definite direttamente in questo file

# File di configurazione e log
CONFIG_FILE = "config.json"
AUTH_LOG_FILE = "/host/var/log/auth.log"  # Percorso al file auth.log all'interno del container
LAST_LOG_POSITION = "/tmp/last_log_position.txt"  # File per memorizzare l'ultima posizione di lettura
ACTIVE_ALERTS_FILE = "/tmp/active_alerts.json"  # File per memorizzare gli alert attivi

# Variabili globali per il bot Telegram
BOT_TOKEN = None
CHAT_ID = None
BOT_INSTANCE = None
UPDATER = None

# Variabili globali per il monitor
last_uptime = 0
EXCLUDED_IPS = ["127.0.0.1", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"]  # Default excluded IPs/ranges

# Cache per i percorsi lunghi (per evitare ButtonDataInvalid)
PATH_CACHE = {}
PATH_COUNTER = 0

# Inizializzazione dell'app Flask
app = Flask(__name__)

# ----------------------------------------
# Funzioni di utilità
# ----------------------------------------

def cache_path(path):
    """Cache per i percorsi lunghi per evitare ButtonDataInvalid"""
    global PATH_CACHE, PATH_COUNTER
    
    # Se il percorso è più lungo di 40 caratteri, lo memorizziamo nella cache
    if len(path) > 40:
        PATH_COUNTER += 1
        cache_id = f"path_{PATH_COUNTER}"
        PATH_CACHE[cache_id] = path
        return cache_id
    return path

def get_cached_path(cache_id_or_path):
    """Recupera un percorso dalla cache o restituisce il percorso diretto"""
    global PATH_CACHE
    
    # Se è un ID della cache, recupera il percorso
    if cache_id_or_path.startswith("path_"):
        return PATH_CACHE.get(cache_id_or_path, "")
    return cache_id_or_path

def run_host_command(command):
    """Esegue un comando sull'host anziché nel container
    
    Utilizzando il socket Docker, è possibile eseguire comandi sull'host
    creando un container privilegiato che condivide i namespace dell'host.
    
    Args:
        command: Lista o stringa del comando da eseguire
        
    Returns:
        Il risultato dell'esecuzione del comando o None in caso di errore
    """
    try:
        if isinstance(command, list):
            cmd_str = " ".join(command)
        else:
            cmd_str = command
        
        # Per i comandi di sistema (reboot/poweroff) utilizziamo nsenter per accedere al namespace dell'host
        if "reboot" in cmd_str or "poweroff" in cmd_str or "shutdown" in cmd_str:
            # nsenter permette di eseguire comandi nei namespace dell'host
            docker_cmd = [
                "docker", "run", "--rm", "--privileged",
                "--pid=host", "--net=host", "--ipc=host",
                "--volume", "/:/host",  # Monta la root dell'host in /host
                "debian:stable-slim",     # Usiamo Debian invece di Alpine
                "chroot", "/host", "sh", "-c", cmd_str  # chroot nella root dell'host
            ]
        else:
            # Per altri comandi, utilizziamo il metodo standard
            docker_cmd = [
                "docker", "run", "--rm", "--privileged", 
                "--pid=host", "--net=host", "--ipc=host",
                "debian:stable-slim", "sh", "-c", cmd_str
            ]
        
        print(f"Esecuzione comando sull'host: {cmd_str}")
        print(f"Comando docker: {' '.join(docker_cmd)}")
        
        result = subprocess.run(docker_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"Comando eseguito con successo sull'host.")
            return result
        else:
            print(f"Errore nell'esecuzione del comando sull'host: {result.stderr}")
            return None
            
    except Exception as e:
        print(f"Errore durante l'esecuzione del comando sull'host: {e}")
        return None

def load_config():
    """Carica la configurazione da file"""
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"Errore nel caricamento della configurazione: {e}")
        # Configurazione di default
        default_config = {
            "excluded_ips": ["127.0.0.1", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"],
            "top_processes": 5,
            "mount_points": [],
            "notify_ssh": True,
            "notify_reboot": True,  # Sempre abilitato, rimossa opzione disabilitazione
            "bot_token": "",
            "chat_id": "",
            "alert_settings": {
                "ssh": {
                    "enabled": True,
                    "reminder_interval": 0,      # 0 = No reminder
                    "notify_recovery": False
                },
                "internet": {
                    "enabled": True,
                    "reminder_interval": 0,     # Non supporta i reminder (impossibile inviare senza connessione)
                    "notify_recovery": True     # Sempre abilitato quando enabled = True
                }
            }
        }
        
        # Crea il file di configurazione se non esiste
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(default_config, f, indent=2)
            print(f"Creato nuovo file di configurazione: {CONFIG_FILE}")
        except Exception as write_error:
            print(f"Impossibile creare il file di configurazione: {write_error}")
            
        return default_config

def check_ip_in_range(ip):
    """Verifica se un indirizzo IP è all'interno dei range esclusi"""
    if not ip:
        return True  # Skip empty IPs
        
    try:
        ip_obj = ipaddress.ip_address(ip)
        for excluded in EXCLUDED_IPS:
            if "/" in excluded:  # This is a network range
                if ip_obj in ipaddress.ip_network(excluded, strict=False):
                    return True
            else:  # This is a single IP
                if ip == excluded:
                    return True
        return False
    except ValueError:
        return True  # In case of invalid IP, skip it

def get_ip_info(ip):
    """Ottiene informazioni su un indirizzo IP da ipinfo.io"""
    try:
        return f"https://ipinfo.io/{ip}"
    except Exception as e:
        print(f"Errore nel recupero delle informazioni IP: {e}")
        return ""

def get_local_ip():
    """Ottiene l'indirizzo IP locale del server"""
    try:
        # Usa hostname -I per ottenere gli indirizzi IP
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
        # Prende il primo IP (solitamente quello principale)
        ip = result.stdout.strip().split()[0]
        return ip
    except Exception as e:
        print(f"Errore nel recupero dell'IP locale: {e}")
        return "unknown"

def get_uptime():
    """Ottiene l'uptime del sistema"""
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.readline().split()[0])
    except:
        try:
            # Fallback per Docker: leggi uptime del sistema host
            with open("/host/proc/uptime", "r") as f:
                return float(f.readline().split()[0])
        except:
            return 0  # Se non riusciamo a leggere l'uptime, restituiamo 0
            
def format_uptime(uptime_seconds):
    """Formatta l'uptime in un formato leggibile"""
    days, remainder = divmod(int(uptime_seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    result = ""
    if days > 0:
        result += f"{days} {'giorni' if days != 1 else 'giorno'}, "
    if hours > 0 or days > 0:
        result += f"{hours} {'ore' if hours != 1 else 'ora'}, "
    if minutes > 0 or hours > 0 or days > 0:
        result += f"{minutes} {'minuti' if minutes != 1 else 'minuto'}, "
    result += f"{seconds} {'secondi' if seconds != 1 else 'secondo'}"
    
    return result

# ----------------------------------------
# Funzioni per il bot Telegram
# ----------------------------------------

def init_bot():
    """Inizializza il bot Telegram"""
    global BOT_INSTANCE, UPDATER, BOT_TOKEN, CHAT_ID
    
    # Carica token e chat ID dalla configurazione
    try:
        config = load_config()
        BOT_TOKEN = config.get("bot_token") or os.getenv("BOT_TOKEN")
        CHAT_ID = config.get("chat_id") or os.getenv("CHAT_ID")
    except Exception as e:
        print(f"Errore nel caricamento della configurazione Telegram: {e}")
    
    if not BOT_INSTANCE and BOT_TOKEN:
        try:
            BOT_INSTANCE = telegram.Bot(token=BOT_TOKEN)
            UPDATER = Updater(token=BOT_TOKEN, use_context=True)
            
            # Registra gli handler per i comandi
            dp = UPDATER.dispatcher
            dp.add_handler(CommandHandler("risorse", command_risorse))
            dp.add_handler(CommandHandler("start", command_start))
            dp.add_handler(CommandHandler("help", command_help))
            dp.add_handler(CommandHandler("reboot", command_reboot))
            dp.add_handler(CommandHandler("shutdown", command_shutdown))
            dp.add_handler(CommandHandler("upload", command_upload))
            # Utilizziamo la funzione Docker
            dp.add_handler(CommandHandler("docker", command_docker))
            dp.add_handler(CallbackQueryHandler(button_callback))
            # Aggiungiamo un handler per i file ricevuti
            dp.add_handler(MessageHandler(Filters.document, handle_file_upload))
            
            # Avvia il polling in un thread separato
            UPDATER.start_polling(drop_pending_updates=True)
            print("Bot Telegram inizializzato con successo")
            return True
        except Exception as e:
            print(f"Errore nell'inizializzazione del bot Telegram: {e}")
            return False
    return bool(BOT_INSTANCE)

def get_resource_keyboard():
    """Costruisce la tastiera inline per i comandi del bot"""
    keyboard = [
        [
            InlineKeyboardButton("CPU", callback_data="cpu_resources"),
            InlineKeyboardButton("RAM", callback_data="ram_resources")
        ],
        [
            InlineKeyboardButton("Disco", callback_data="disk_resources"),
            InlineKeyboardButton("Rete", callback_data="network_resources")
        ],
        [
            InlineKeyboardButton("Docker List", callback_data="docker_list")
        ],
        [
            InlineKeyboardButton("Tutti", callback_data="all_resources")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def command_risorse(update, context):
    """Handler per il comando /risorse"""
    update.message.reply_text(
        "Scegli quale informazione visualizzare:",
        reply_markup=get_resource_keyboard()
    )

def command_start(update, context):
    """Handler per il comando /start"""
    update.message.reply_text(
        "Benvenuto nel Server Monitor Bot!\n\n"
        "Questo bot ti permette di monitorare lo stato del tuo server e ricevere notifiche "
        "quando vengono rilevati eventi importanti come accessi SSH o utilizzo elevato delle risorse.\n\n"
        "Usa /risorse per controllare lo stato attuale del server\n"
        "Usa /help per vedere tutti i comandi disponibili"
    )

def command_help(update, context):
    """Handler per il comando /help"""
    update.message.reply_text(
        "Comandi disponibili:\n\n"
        "/start - Avvia il bot\n"
        "/help - Mostra questo messaggio di aiuto\n"
        "/risorse - Visualizza le risorse del sistema\n"
        "/docker - Gestisci i container Docker\n"
        "/upload - Carica files sul server\n"
        "/reboot - Riavvia il server (richiede conferma)\n"
        "/shutdown - Spegne il server (richiede conferma)\n"
    )

def command_reboot(update, context):
    """Handler per il comando /reboot"""
    keyboard = [
        [InlineKeyboardButton("Sì, riavvia il server", callback_data="confirm_reboot")],
        [InlineKeyboardButton("No, annulla", callback_data="cancel_action")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    update.message.reply_text(
        "*Sei sicuro di voler riavviare il server?*\n\n"
        "Questa azione causerà un'interruzione temporanea di tutti i servizi.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

def command_shutdown(update, context):
    """Handler per il comando /shutdown"""
    keyboard = [
        [InlineKeyboardButton("Sì, spegni il server", callback_data="confirm_shutdown")],
        [InlineKeyboardButton("No, annulla", callback_data="cancel_action")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    update.message.reply_text(
        "*Sei sicuro di voler spegnere il server?*\n\n"
        "Questa azione causerà un'interruzione di tutti i servizi fino al prossimo avvio manuale.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

# ... [Altri command handlers da mantenere] ...

def button_callback(update, context):
    """Gestisce i callback dai pulsanti inline"""
    query = update.callback_query
    query.answer()
    
    data = query.data
    
    if data == "cpu_resources":
        cpu_info = get_cpu_resources()
        query.edit_message_text(text=cpu_info, parse_mode="Markdown")
    
    elif data == "ram_resources":
        ram_info = get_ram_resources()
        query.edit_message_text(text=ram_info, parse_mode="Markdown")
    
    elif data == "system_resources":  # manteniamo per retrocompatibilità
        # Combina CPU e RAM per retrocompatibilità
        cpu_info = get_cpu_resources()
        ram_info = get_ram_resources()
        query.edit_message_text(text=f"{cpu_info}\n\n{ram_info}", parse_mode="Markdown")
    
    elif data == "disk_resources":
        disk_info = get_disk_info()
        query.edit_message_text(text=disk_info, parse_mode="Markdown")
    
    elif data == "network_resources":
        # Inizializza la misurazione della velocità di rete se necessario
        global last_net_io, last_net_io_time
        if last_net_io is None:
            last_net_io = psutil.net_io_counters()
            last_net_io_time = time.time()
            time.sleep(1)  # Attendi un secondo per avere una misurazione iniziale
            
        net_info = get_network_info()
        query.edit_message_text(text=net_info, parse_mode="Markdown")
    
    elif data.startswith("top_processes_"):
        num = int(data.split("_")[-1])
        processes = get_top_processes(num)
        query.edit_message_text(text=processes, parse_mode="Markdown")
    
    elif data == "docker_list":
        # Usa la funzione Docker list - solo visualizzazione
        docker_list(query, context)
    
    elif data == "all_resources":
        # Raccoglie tutte le informazioni
        cpu_info = get_cpu_resources()
        ram_info = get_ram_resources()
        disk_info = get_disk_info()
        net_info = get_network_info()
        
        # Combina le informazioni principali in un unico messaggio
        all_info = (f"*Sistema - Panoramica*\n\n"
                   f"CPU: *{psutil.cpu_percent()}%*\n"
                   f"RAM: *{psutil.virtual_memory().percent}%*\n"
                   f"Disco Root: *{psutil.disk_usage('/').percent}%*\n\n"
                   f"{cpu_info}\n\n{ram_info}\n\n{disk_info}\n\n{net_info}")
        query.edit_message_text(text=all_info, parse_mode="Markdown")
    
    # Aggiungi il pulsante per tornare al menu principale solo se non è già una richiesta di tornare al menu
    if data != "back_to_menu":
        try:
            query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Torna al menu", callback_data="back_to_menu")]
                ])
            )
        except Exception as markup_error:
            print(f"Impossibile aggiornare la markup: {markup_error}")
    
    if data == "back_to_menu":
        try:
            # Modificare il messaggio con contenuto sempre diverso usando un timestamp
            import datetime
            import random
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            random_id = random.randint(1000, 9999)  # Un numero casuale per garantire la differenza
            
            # Prova a modificare il messaggio con un nuovo testo e tastiera
            query.edit_message_text(
                text=f"Scegli quale informazione visualizzare: [{timestamp}-{random_id}]",
                reply_markup=get_resource_keyboard(),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Errore nella gestione del pulsante 'torna al menu': {e}")
            # Se la modifica fallisce, elimina e invia un nuovo messaggio
            try:
                query.message.delete()
                context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="Scegli quale informazione visualizzare:",
                    reply_markup=get_resource_keyboard()
                )
            except Exception as e2:
                print(f"Errore nel tentativo di ripristino: {e2}")
                
    elif data == "confirm_reboot":
        # Conferma riavvio server
        query.edit_message_text(
            text="*Riavvio del server in corso...*\n\nIl server verrà riavviato entro pochi secondi.",
            parse_mode="Markdown"
        )
        
        # Esegui il comando di riavvio
        import subprocess, threading
        def delayed_reboot():
            import time
            time.sleep(3)  # Aspetta 3 secondi per permettere l'invio della risposta
            try:
                # Lista dei comandi di riavvio da provare tramite run_host_command
                host_commands = [
                    "reboot now",
                    "/sbin/reboot now",
                    "systemctl reboot",
                    "shutdown -r now"
                ]
                
                # Prova tutti i comandi di riavvio tramite run_host_command
                for cmd_str in host_commands:
                    print(f"Tentativo di riavvio dell'host con: {cmd_str}")
                    host_result = run_host_command(cmd_str)
                    if host_result and host_result.returncode == 0:
                        print(f"Riavvio dell'host eseguito con successo tramite Docker con comando: {cmd_str}")
                        return
                    else:
                        print(f"Fallito riavvio con comando: {cmd_str}")
                
                # Se i comandi tramite Docker falliscono, prova comandi locali (meno probabili che funzionino in container)
                print("Tutti i tentativi via Docker falliti, provo comandi diretti...")
                commands = [
                    ["sudo", "reboot", "now"],
                    ["sudo", "/sbin/reboot", "now"],
                    ["sudo", "systemctl", "reboot"],
                    ["sudo", "shutdown", "-r", "now"]
                ]
                
                for cmd in commands:
                    try:
                        print(f"Tentativo di riavvio con comando diretto: {' '.join(cmd)}")
                        result = subprocess.run(cmd, capture_output=True, text=True)
                        if result.returncode == 0:
                            print("Comando di riavvio eseguito con successo")
                            return
                        else:
                            print(f"Errore nell'esecuzione del comando: {result.stderr}")
                    except Exception as cmd_error:
                        print(f"Errore durante l'esecuzione del comando {cmd}: {cmd_error}")
                        
                # Se arriviamo qui, tutti i tentativi sono falliti
                print("ATTENZIONE: Tutti i tentativi di riavvio sono falliti")
                
            except Exception as e:
                print(f"Errore durante il tentativo di riavvio: {e}")
            
        # Avvia il riavvio in un thread separato
        threading.Thread(target=delayed_reboot).start()
    
    elif data == "confirm_shutdown":
        # Conferma spegnimento server
        query.edit_message_text(
            text="*Spegnimento del server in corso...*\n\nIl server verrà spento entro pochi secondi.",
            parse_mode="Markdown"
        )
        
        # Esegui il comando di spegnimento
        import subprocess, threading
        def delayed_shutdown():
            import time
            time.sleep(3)  # Aspetta 3 secondi per permettere l'invio della risposta
            try:
                # Lista dei comandi di spegnimento da provare tramite run_host_command
                host_commands = [
                    "poweroff",
                    "/sbin/poweroff",
                    "shutdown -h now",
                    "systemctl poweroff",
                    "halt -p"  # L'opzione -p indica di spegnere fisicamente
                ]
                
                # Prova tutti i comandi di spegnimento tramite run_host_command
                for cmd_str in host_commands:
                    print(f"Tentativo di spegnimento dell'host con: {cmd_str}")
                    host_result = run_host_command(cmd_str)
                    if host_result and host_result.returncode == 0:
                        print(f"Spegnimento dell'host eseguito con successo tramite Docker con comando: {cmd_str}")
                        return
                    else:
                        print(f"Fallito spegnimento con comando: {cmd_str}")
                
                # Se i comandi tramite Docker falliscono, prova comandi locali (meno probabili che funzionino in container)
                print("Tutti i tentativi via Docker falliti, provo comandi diretti...")
                commands = [
                    ["sudo", "poweroff"],
                    ["sudo", "shutdown", "-h", "now"],
                    ["sudo", "/sbin/poweroff"],
                    ["sudo", "systemctl", "poweroff"],
                    ["sudo", "halt", "-p"]
                ]
                
                for cmd in commands:
                    try:
                        print(f"Tentativo di spegnimento con comando diretto: {' '.join(cmd)}")
                        result = subprocess.run(cmd, capture_output=True, text=True)
                        if result.returncode == 0:
                            print("Comando di spegnimento eseguito con successo")
                            return
                        else:
                            print(f"Errore nell'esecuzione del comando: {result.stderr}")
                    except Exception as cmd_error:
                        print(f"Errore durante l'esecuzione del comando {cmd}: {cmd_error}")
                        
                # Se arriviamo qui, tutti i tentativi sono falliti
                print("ATTENZIONE: Tutti i tentativi di spegnimento sono falliti")
                
            except Exception as e:
                print(f"Errore durante il tentativo di spegnimento: {e}")
            
        # Avvia lo spegnimento in un thread separato
        threading.Thread(target=delayed_shutdown).start()
    
    elif data == "cancel_action":
        # Annulla l'azione
        query.edit_message_text(
            text="Azione annullata.",
            parse_mode="Markdown"
        )
        
    # Gestione dei callback per l'upload di file
    elif data.startswith("browse_dir_"):
        # Gestione della navigazione directory
        handle_directory_browsing(query, context, data)
        
    elif data.startswith("select_dir_"):
        # Gestione della selezione della directory corrente per upload
        handle_directory_selection(query, context, data)
        
    elif data.startswith("parent_dir_"):
        # Navigazione verso la directory padre
        handle_parent_directory(query, context, data)
        
    elif data.startswith("prev_dir_"):
        # Navigazione verso la directory precedente
        handle_previous_directory(query, context, data)
        
    elif data == "create_dir":
        # Richiesta di creazione nuova directory
        handle_create_directory_request(query, context)
        
    elif data == "upload_cancel":
        # Annulla l'upload
        global UPLOAD_STATES
        chat_id = query.message.chat_id
        
        if chat_id in UPLOAD_STATES:
            del UPLOAD_STATES[chat_id]
        
        query.edit_message_text(
            text="❌ Upload annullato.",
            parse_mode="Markdown"
        )
        
    elif data == "upload_restart":
        # Ricomincia l'upload dall'inizio
        command_upload(query, context)
        
    elif data == "upload_continue":
        # Continua ad uploadare altri file
        chat_id = query.message.chat_id
        if chat_id in UPLOAD_STATES:
            UPLOAD_STATES[chat_id]["state"] = "uploading"
            
            # Aggiorna il messaggio
            query.edit_message_text(
                text=f"Carica i files che desideri salvare in *{UPLOAD_STATES[chat_id]['dir']}*\n\n" +
                     "Puoi inviare più file. Quando hai finito, premi Fine.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Fine", callback_data="upload_finish")]])
            )
        else:
            query.edit_message_text(
                text="⚠️ Sessione di upload scaduta. Usa /upload per iniziare nuovamente.",
                parse_mode="Markdown"
            )
            
    elif data == "upload_finish":
        # Termina l'upload
        chat_id = query.message.chat_id
        
        if chat_id in UPLOAD_STATES:
            upload_dir = UPLOAD_STATES[chat_id].get("dir", "destinazione sconosciuta")
            del UPLOAD_STATES[chat_id]
            
            query.edit_message_text(
                text=f"✅ *Upload completato*\n\nI files sono stati salvati in: {upload_dir}",
                parse_mode="Markdown"
            )
        else:
            query.edit_message_text(
                text="⚠️ Sessione di upload terminata o scaduta.",
                parse_mode="Markdown"
            )
            
    elif data == "upload_restart":
        # Ricomincia il processo di upload dal principio
        command_upload(query, context)

    elif data.startswith("docker_") and not data.startswith("docker_start_") and not data.startswith("docker_stop_") and not data.startswith("docker_restart_") and not data.startswith("docker_pause_") and not data.startswith("docker_kill_") and data != "docker_back" and data != "docker_list" and data != "docker_manage":
        # Gestione container Docker - visualizza dettagli e azioni possibili
        command_docker(query, context)
        
    elif data == "docker_back":
        # Torna al menu Docker
        command_docker(query, context)

def load_active_alerts():
    """Carica gli alert attivi dal file"""
    try:
        if os.path.exists(ACTIVE_ALERTS_FILE):
            with open(ACTIVE_ALERTS_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception as e:
        print(f"Errore nel caricamento degli alert attivi: {e}")
        return {}

def save_active_alerts(alerts):
    """Salva gli alert attivi nel file"""
    try:
        # Assicurati che la directory esista
        if not os.path.exists(os.path.dirname(ACTIVE_ALERTS_FILE)):
            try:
                os.makedirs(os.path.dirname(ACTIVE_ALERTS_FILE))
            except Exception as e:
                print(f"Errore nella creazione della directory: {e}")
                return False
                
        with open(ACTIVE_ALERTS_FILE, 'w') as f:
            json.dump(alerts, f, indent=2)
        return True
    except Exception as e:
        print(f"Errore nel salvataggio degli alert attivi: {e}")
        return False

def send_alert(message, alert_type="generic", alert_key=None, is_recovery=False, force=False):
    """Invia un messaggio di avviso tramite Telegram con gestione degli alert attivi"""
    max_retries = 3
    retry_delay = 2
    
    # Carica la configurazione per gli alert
    config = load_config()
    alert_settings = config.get("alert_settings", {})
    
    # Debug per vedere cosa sta succedendo con le notifiche
    alert_enabled = alert_settings.get(alert_type, {}).get('enabled', True)
    notify_recovery_enabled = alert_settings.get(alert_type, {}).get('notify_recovery', True)
    print(f"[DEBUG] Sending alert of type '{alert_type}' with key '{alert_key}', enabled={alert_enabled}, notify_recovery={notify_recovery_enabled}, is_recovery={is_recovery}")
    
    # Se è un alert di tipo conosciuto, controlla se è abilitato
    if alert_type in alert_settings:
        # Usa il valore esplicito dalle impostazioni degli alert
        if not alert_enabled:
            print(f"Alert di tipo '{alert_type}' disabilitato dalla configurazione.")
            return False
    
    # Se è un alert di recupero, controlla se le notifiche di recupero sono abilitate
    if is_recovery and alert_type in alert_settings:
        notify_recovery = alert_settings[alert_type].get("notify_recovery", True)
        print(f"[DEBUG] Notifica di recupero per '{alert_type}': abilitata={notify_recovery}")
        if not notify_recovery:
            print(f"Notifica di recupero per '{alert_type}' disabilitata dalla configurazione.")
            return False
    
    # Gestione dello stato degli alert
    active_alerts = load_active_alerts()
    current_time = time.time()
    
    # Crea una chiave univoca per l'alert se non specificata
    if not alert_key:
        alert_key = f"{alert_type}_{hash(message) % 10000}"
    
    # Controlla se è un alert di recupero
    if is_recovery:
        # Se è un recupero ma l'alert non è attivo, non fare nulla
        if alert_key not in active_alerts:
            print(f"Nessun alert attivo trovato per il recupero di {alert_key}")
            return False
        
        # Rimuovi l'alert dalle attive
        alert_info = active_alerts.pop(alert_key)
        save_active_alerts(active_alerts)
        print(f"[DEBUG] Alert rimosso dagli attivi: {alert_key}")
    else:
        # Se l'alert è già attivo, controlla se è necessario un reminder o se è forzato
        if alert_key in active_alerts and not force:
            alert_info = active_alerts[alert_key]
            last_notification = alert_info.get("last_notification", 0)
            reminder_interval = alert_settings.get(alert_type, {}).get("reminder_interval", 3600)  # Default 1 ora
            
            # Se l'intervallo è 0, non inviare reminder
            if reminder_interval == 0:
                print(f"Reminder disabilitati per alert di tipo '{alert_type}'")
                return False
                
            # Se non è passato abbastanza tempo dall'ultimo reminder, non inviare
            if current_time - last_notification < reminder_interval:
                time_elapsed = current_time - last_notification
                time_to_next = reminder_interval - time_elapsed
                print(f"Alert '{alert_key}' già attivo. Prossimo reminder tra {time_to_next:.0f} secondi.")
                return False
        elif alert_key in active_alerts and force:
            print(f"Invio forzato dell'alert '{alert_key}' anche se già attivo")
            
            # Aggiorna il timestamp dell'ultimo reminder
            alert_info = active_alerts[alert_key]
            alert_info["last_notification"] = current_time
            alert_info["reminder_count"] = alert_info.get("reminder_count", 0) + 1
            active_alerts[alert_key] = alert_info
            
            # Modifica il messaggio per indicare che è un reminder
            message = f"🔄 REMINDER ({alert_info['reminder_count']}) - {message}"
        else:
            # Nuovo alert, registralo come attivo
            active_alerts[alert_key] = {
                "type": alert_type,
                "message": message,
                "start_time": current_time,
                "last_notification": current_time,
                "reminder_count": 0
            }
        
        # Salva lo stato aggiornato degli alert
        save_active_alerts(active_alerts)
    
    # Inizializza il bot se non è già stato fatto
    if not init_bot():
        print("ERRORE: Impossibile inizializzare il bot Telegram")
        return False
    
    # Invia l'alert tramite Telegram
    for attempt in range(max_retries):
        try:
            print(f"Invio messaggio Telegram: {message}")
            
            # Verifica che il token e il chat ID siano impostati
            if not BOT_TOKEN or BOT_TOKEN == "token":
                print("ERRORE: BOT_TOKEN non configurato correttamente")
                return False
                
            if not CHAT_ID or CHAT_ID == "id":
                print("ERRORE: CHAT_ID non configurato correttamente")
                return False
            
            result = BOT_INSTANCE.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")
            print(f"Messaggio inviato con successo: {result}")
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"Errore invio messaggio Telegram (tentativo {attempt+1}/{max_retries}): {e}")
                time.sleep(retry_delay)
            else:
                print(f"Errore invio messaggio Telegram (tutti i tentativi falliti): {e}")
                return False

def send_recovery_alert(alert_type, alert_key, custom_message=None):
    """Invia una notifica di recupero per un alert attivo"""
    active_alerts = load_active_alerts()
    
    print(f"[DEBUG] Tentativo di invio notifica di ripristino: tipo={alert_type}, chiave={alert_key}")
    
    if alert_key in active_alerts:
        alert_info = active_alerts[alert_key]
        start_time = alert_info.get("start_time", time.time())
        elapsed_time = time.time() - start_time
        
        # Debug: mostra informazioni sull'alert attivo
        print(f"[DEBUG] Alert recupero: chiave={alert_key}, tipo={alert_type}, tipo originale={alert_info.get('type', 'sconosciuto')}")
        
        # Formatta il tempo trascorso in modo leggibile
        hours, remainder = divmod(int(elapsed_time), 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = ""
        if hours > 0:
            duration_str += f"{hours}h "
        if minutes > 0 or hours > 0:
            duration_str += f"{minutes}m "
        duration_str += f"{seconds}s"
        
        if custom_message:
            message = f"✅ RISOLTO - {custom_message} (durata: {duration_str})"
        else:
            message = f"✅ RISOLTO - {alert_info.get('message', 'Alert sconosciuto')} (durata: {duration_str})"
        
        # Importante: passa il tipo originale dell'alert da ripristinare
        # Se il tipo originale esiste, lo usiamo per la compatibilità
        original_type = alert_info.get("type")
        if original_type:
            print(f"[DEBUG] Sovrascrivendo tipo alert con quello originale per la notifica di ripristino: {original_type}")
            alert_type = original_type
            
        # Forza l'invio della notifica indipendentemente dalle impostazioni
        result = send_alert(message, alert_type, alert_key, is_recovery=True, force=True)
        print(f"[DEBUG] Risultato invio notifica di recupero per {alert_key}: {result}")
        return result
    else:
        print(f"Nessun alert attivo trovato con chiave {alert_key}")
        return False

# ----------------------------------------
# Funzioni per il monitoraggio delle risorse
# ----------------------------------------

# Variabili globali per il monitoraggio della connessione internet
INTERNET_CONNECTED = True
INTERNET_DISCONNECTION_TIME = None

# Variabili globali per la gestione degli upload di file
UPLOAD_STATES = {}  # Dizionario per gestire lo stato di upload per ogni utente {chat_id: {"state": "...", "dir": "...", ...}}

def check_internet_connection():
    """Verifica se c'è connessione a internet provando a contattare diversi servizi"""
    global INTERNET_CONNECTED, INTERNET_DISCONNECTION_TIME
    
    # Lista di host affidabili da provare
    reliable_hosts = [
        "8.8.8.8",   # Google DNS
        "1.1.1.1",   # Cloudflare DNS
        "208.67.222.222"  # OpenDNS
    ]
    
    # Porta da usare (53 = DNS)
    port = 53
    timeout = 3
    
    for host in reliable_hosts:
        try:
            # Proviamo a stabilire una connessione socket
            socket.setdefaulttimeout(timeout)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            
            # Se siamo qui, la connessione è riuscita
            # Controlliamo se era già disconnessa e inviamo notifica di ripristino
            if not INTERNET_CONNECTED and INTERNET_DISCONNECTION_TIME is not None:
                current_time = time.time()
                downtime_seconds = current_time - INTERNET_DISCONNECTION_TIME
                
                # Formatta il tempo di disconnessione
                downtime_formatted = format_uptime(downtime_seconds)
                
                # Invia notifica di ripristino
                config = load_config()
                alert_settings = config.get("alert_settings", {})
                internet_settings = alert_settings.get("internet", {})
                
                # Se le notifiche internet sono abilitate, invia sempre la notifica di ripristino
                if internet_settings.get("enabled", True):
                    send_recovery_alert(
                        "internet", 
                        "internet_connection", 
                        f"Connessione internet ripristinata dopo {downtime_formatted} di disconnessione"
                    )
                
                # Resetta il timer di disconnessione
                INTERNET_DISCONNECTION_TIME = None
            
            # La connessione è attiva
            INTERNET_CONNECTED = True
            return True
        except Exception as e:
            continue
    
    # Se siamo qui, tutti i tentativi sono falliti
    # Se è la prima volta che rileviamo la disconnessione, registra il timestamp
    if INTERNET_CONNECTED:
        INTERNET_CONNECTED = False
        INTERNET_DISCONNECTION_TIME = time.time()
        
        # Invia notifica di disconnessione solo se la connessione è appena stata persa
        # Non ha senso inviare reminder per la perdita di connessione perché non possono essere consegnati
        config = load_config()
        alert_settings = config.get("alert_settings", {})
        internet_settings = alert_settings.get("internet", {})
        
        # Invia la notifica solo se è abilitata
        if internet_settings.get("enabled", True):
            # L'invio della notifica potrebbe fallire se la connessione è già persa
            try:
                send_alert("⚠️ CONNESSIONE INTERNET PERSA", alert_type="internet", alert_key="internet_connection")
            except Exception as e:
                print(f"Impossibile inviare notifica di perdita connessione: {e} - La connessione potrebbe essere già compromessa")
    
    return False

# Funzioni per la visualizzazione delle risorse di sistema (mantenute per compatibilità)
def get_cpu_resources():
    """Ottiene informazioni dettagliate sulla CPU"""
    try:
        # Utilizzo CPU totale e per tipo
        cpu_total = psutil.cpu_percent(interval=1)
        cpu_times_percent = psutil.cpu_times_percent(interval=1)
        
        # Numero di core
        cpu_count = psutil.cpu_count(logical=True)
        cpu_count_physical = psutil.cpu_count(logical=False)
        
        # Ottiene il carico di sistema (1, 5, 15 minuti)
        try:
            load_avg = os.getloadavg()
            load_str = f"1 min: *{load_avg[0]:.2f}*\n5 min: *{load_avg[1]:.2f}*\n15 min: *{load_avg[2]:.2f}*"
        except:
            load_str = "non disponibile"
        
        # Uptime del sistema
        uptime_seconds = get_uptime()
        days, remainder = divmod(int(uptime_seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"
        
        return (f"*Informazioni CPU*\n"
                f"Utilizzo totale: *{cpu_total}%*\n\n"
                f"*Dettaglio utilizzo:*\n"
                f"user: *{cpu_times_percent.user:.1f}%*\n"
                f"system: *{cpu_times_percent.system:.1f}%*\n"
                f"idle: *{cpu_times_percent.idle:.1f}%*\n"
                f"iowait: *{getattr(cpu_times_percent, 'iowait', 0):.1f}%*\n\n"
                f"*Cores:* {cpu_count} logici ({cpu_count_physical} fisici)\n\n"
                f"*Load Average* ({cpu_count_physical}-core)\n"
                f"{load_str}"
                f"\n\n*Uptime:* {uptime_str}")
    except Exception as e:
        return f"Errore nel recupero delle informazioni CPU: {e}"

def get_ram_resources():
    """Ottiene informazioni dettagliate sulla RAM e swap"""
    try:
        # Informazioni memoria RAM
        ram = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        # Converti i byte in GB per maggiore leggibilità
        ram_total_gb = ram.total / (1024**3)
        ram_used_gb = ram.used / (1024**3)
        ram_free_gb = ram.free / (1024**3)
        ram_active_gb = getattr(ram, 'active', 0) / (1024**3)
        ram_inactive_gb = getattr(ram, 'inactive', 0) / (1024**3)
        ram_buffers_gb = getattr(ram, 'buffers', 0) / (1024**3)
        ram_cached_gb = getattr(ram, 'cached', 0) / (1024**3)
        
        # Informazioni memoria swap
        swap_total_gb = swap.total / (1024**3)
        swap_used_gb = swap.used / (1024**3)
        swap_free_gb = swap.free / (1024**3)
        
        # Ottiene il carico di sistema (1, 5, 15 minuti)
        try:
            load_avg = os.getloadavg()
            cpu_count = psutil.cpu_count(logical=False) or 1
            load_str = f"*LOAD* ({cpu_count}-core)\n1 min: *{load_avg[0]:.2f}*\n5 min: *{load_avg[1]:.2f}*\n15 min: *{load_avg[2]:.2f}*"
        except:
            load_str = "Load average: non disponibile"
        
        return (f"*Informazioni Memoria*\n\n"
                f"*RAM:* {ram.percent}%\n"
                f"total: *{ram_total_gb:.1f}G*\n"
                f"used: *{ram_used_gb:.1f}G*\n"
                f"free: *{ram_free_gb:.1f}G*\n"
                f"active: *{ram_active_gb:.2f}G*\n"
                f"inactive: *{ram_inactive_gb:.2f}G*\n"
                f"buffers: *{ram_buffers_gb:.2f}G*\n"
                f"cached: *{ram_cached_gb:.2f}G*\n\n"
                f"*SWAP:* {swap.percent}%\n"
                f"total: *{swap_total_gb:.1f}G*\n"
                f"used: *{swap_used_gb:.1f}G*\n"
                f"free: *{swap_free_gb:.1f}G*\n\n"
                f"{load_str}")
    except Exception as e:
        return f"Errore nel recupero delle informazioni sulla memoria: {e}"

def get_disk_info():
    """Ottiene informazioni sull'utilizzo del disco"""
    try:
        # Carica la configurazione per i mount points da monitorare
        config = load_config()
        mount_points = config.get("mount_points", [])
        monitored_mount_points = [mount["path"] for mount in mount_points] if mount_points else ["/"]
        
        # Informazioni sul disco root
        disk = psutil.disk_usage("/")
        
        # Formatta le dimensioni in GB
        used_gb = disk.used / (1024**3)
        total_gb = disk.total / (1024**3)
        free_gb = disk.free / (1024**3)
        
        # Ottiene le partizioni
        partitions = psutil.disk_partitions()
        partitions_info = ""
        
        # Monitora solo i mount points specificati nella configurazione
        monitored_info = ""
        for path in monitored_mount_points:
            try:
                usage = psutil.disk_usage(path)
                monitored_info += (f"\n{path}: "
                               f"{usage.percent}% usato "
                               f"({usage.used / (1024**3):.1f} GB / {usage.total / (1024**3):.1f} GB)")
            except Exception as e:
                monitored_info += f"\n{path}: Errore nel leggere le informazioni - {str(e)}"
        
        # Non aggiungiamo altre partizioni, mostriamo solo quelle monitorate
            
        # Verifichiamo se ci sono mount points monitorati
        if monitored_mount_points:
            return (f"*Informazioni Disco*\n"
                    f"Root Usage: *{disk.percent}%*\n"
                    f"Usato: {used_gb:.1f} GB\n"
                    f"Libero: {free_gb:.1f} GB\n"
                    f"Totale: {total_gb:.1f} GB\n"
                    f"*Mount Points Monitorati*:{monitored_info}")
        else:
            return (f"*Informazioni Disco*\n"
                    f"Root Usage: *{disk.percent}%*\n"
                    f"Usato: {used_gb:.1f} GB\n"
                    f"Libero: {free_gb:.1f} GB\n"
                    f"Totale: {total_gb:.1f} GB\n\n"
                    f"Nessun mount point configurato. Aggiungili dalla GUI.")
    except Exception as e:
        return f"Errore nel recupero delle informazioni sul disco: {e}"

# Variabili per calcolare la velocità di rete
last_net_io = None
last_net_io_time = 0

def get_network_info():
    """Ottiene informazioni sul traffico di rete"""
    global last_net_io, last_net_io_time
    
    try:
        # Ottieni le statistiche di rete
        net_io = psutil.net_io_counters()
        current_time = time.time()
        
        # Converti in formato leggibile
        sent_mb = net_io.bytes_sent / (1024**2)
        recv_mb = net_io.bytes_recv / (1024**2)
        
        # Calcola la velocità di download e upload se abbiamo dati precedenti
        speed_info = ""
        if last_net_io and last_net_io_time > 0:
            # Calcola il tempo trascorso in secondi
            time_delta = current_time - last_net_io_time
            
            # Calcola i byte inviati e ricevuti nel periodo
            bytes_sent_delta = net_io.bytes_sent - last_net_io.bytes_sent
            bytes_recv_delta = net_io.bytes_recv - last_net_io.bytes_recv
            
            # Calcola la velocità in KB/s
            upload_speed = bytes_sent_delta / time_delta / 1024
            download_speed = bytes_recv_delta / time_delta / 1024
            
            # Formatta la velocità in unità appropriate (KB/s o MB/s)
            if upload_speed > 1024:
                upload_speed_str = f"{upload_speed / 1024:.2f} MB/s"
            else:
                upload_speed_str = f"{upload_speed:.2f} KB/s"
                
            if download_speed > 1024:
                download_speed_str = f"{download_speed / 1024:.2f} MB/s"
            else:
                download_speed_str = f"{download_speed:.2f} KB/s"
            
            speed_info = f"\nVelocità Download: *{download_speed_str}*\nVelocità Upload: *{upload_speed_str}*"
        
        # Aggiorna i dati per il prossimo calcolo
        last_net_io = net_io
        last_net_io_time = current_time
        
        # Ottieni le connessioni attive
        connections = psutil.net_connections()
        established = sum(1 for conn in connections if conn.status == 'ESTABLISHED')
        listen = sum(1 for conn in connections if conn.status == 'LISTEN')
        
        # Ottieni le informazioni sulle interfacce di rete
        net_if = psutil.net_if_addrs()
        interfaces = []
        
        for interface, addresses in net_if.items():
            for addr in addresses:
                if addr.family == socket.AF_INET:  # Solo IPv4
                    interfaces.append(f"{interface}: {addr.address}")
                    break
        
        return (f"*Informazioni Rete*\n"
                f"Dati inviati: {sent_mb:.2f} MB\n"
                f"Dati ricevuti: {recv_mb:.2f} MB\n"
                f"{speed_info}\n"
                f"Connessioni stabilite: {established}\n"
                f"Porte in ascolto: {listen}\n"
                f"*Interfacce*:\n" + "\n".join(interfaces[:5]))  # Limita a 5 interfacce
    except Exception as e:
        return f"Errore nel recupero delle informazioni di rete: {e}"

def check_auth_log():
    """Monitora il file auth.log per individuare nuovi accessi SSH"""
    print("Controllo nuovi accessi SSH da auth.log...")
    
    # Verifica che il file di log esista
    if not os.path.exists(AUTH_LOG_FILE):
        print(f"File {AUTH_LOG_FILE} non trovato. Controlla il volume montato.")
        return

    # Determina da quale posizione iniziare a leggere il file
    last_position = 0
    if os.path.exists(LAST_LOG_POSITION):
        try:
            with open(LAST_LOG_POSITION, 'r') as f:
                last_position = int(f.read().strip() or '0')
        except Exception as e:
            print(f"Errore nella lettura dell'ultima posizione: {e}")
    
    # Ottieni la dimensione attuale del file
    current_size = os.path.getsize(AUTH_LOG_FILE)
    
    # Se il file è stato ruotato o troncato (dimensione minore dell'ultima posizione), ricomincia da zero
    if current_size < last_position:
        last_position = 0
    
    # Leggi solo le nuove righe dal file
    with open(AUTH_LOG_FILE, 'r') as f:
        f.seek(last_position)
        new_lines = f.readlines()
        
        # Aggiorna la posizione dell'ultima lettura
        with open(LAST_LOG_POSITION, 'w') as pos_file:
            pos_file.write(str(f.tell()))
    
    # Pattern per trovare i log di accesso SSH
    ssh_pattern = re.compile(r'(\w+\s+\d+\s+\d+:\d+:\d+)\s+(\S+)\s+sshd\[\d+\]:\s+Accepted\s+\S+\s+for\s+(\S+)\s+from\s+(\S+)')
    
    for line in new_lines:
        match = ssh_pattern.search(line)
        if match:
            # Estrazione delle informazioni
            timestamp_str, hostname, username, source_ip = match.groups()
            
            # Controlla se l'IP è nella lista degli esclusi
            if not check_ip_in_range(source_ip):
                # Ottieni timestamp formattato
                try:
                    # Aggiungi l'anno attuale poiché il log non lo include
                    current_year = datetime.now().year
                    full_timestamp_str = f"{timestamp_str} {current_year}"
                    # Converti in oggetto datetime
                    timestamp = datetime.strptime(full_timestamp_str, "%b %d %H:%M:%S %Y")
                    formatted_date = timestamp.strftime("%d %b %Y %H:%M")
                except Exception as e:
                    print(f"Errore nella formattazione della data: {e}")
                    formatted_date = timestamp_str
                
                # Ottieni l'indirizzo IP locale
                try:
                    local_ip = get_local_ip()
                except Exception as e:
                    local_ip = "unknown"
                    print(f"Errore nel recupero dell'IP locale: {e}")
                
                # Preparazione del messaggio
                message = (f"*SSH Connection detected*\n"
                           f"Connection from *{source_ip}* as *{username}* on *{hostname}* ({local_ip})\n"
                           f"Date: {formatted_date}\n"
                           f"More information: {get_ip_info(source_ip)}")
                
                print(f"Nuovo accesso SSH rilevato: {username} da {source_ip} su {hostname}")
                # Usa alert_type="ssh" per rispettare le impostazioni di notifica SSH
                send_alert(message, alert_type="ssh")
            else:
                print(f"Accesso SSH da {source_ip} escluso dalle notifiche.")

def monitor_loop():
    """Ciclo principale di monitoraggio"""
    global last_uptime, EXCLUDED_IPS
    
    # Crea i file di stato se non esistono
    if not os.path.exists(os.path.dirname(LAST_LOG_POSITION)):
        try:
            os.makedirs(os.path.dirname(LAST_LOG_POSITION))
        except:
            pass
    
    try:
        if not os.path.exists(LAST_LOG_POSITION):
            with open(LAST_LOG_POSITION, "w") as f:
                f.write("0")
    except:
        print(f"Errore nella creazione del file {LAST_LOG_POSITION}")
    
    config = load_config()
    try:
        # Inizializza l'uptime con un valore molto piccolo per evitare falsi positivi al primo avvio
        last_uptime = 0.1  # Un valore molto piccolo ma maggiore di zero
        # Dopo un breve ritardo leggiamo il vero uptime per evitare falsi positivi
        time.sleep(2)
        last_uptime = get_uptime()
        print(f"Uptime iniziale: {last_uptime} secondi")
    except Exception as e:
        print(f"Errore nel leggere l'uptime: {e}")
        last_uptime = 0

    # Imposta gli IP esclusi all'avvio
    if "excluded_ips" in config:
        EXCLUDED_IPS = config["excluded_ips"]

    print("Monitor loop avviato.")
    last_check_time = 0
    
    while True:
        try:
            config = load_config()
            
            # Aggiorna la lista degli IP esclusi ad ogni ciclo
            if "excluded_ips" in config:
                EXCLUDED_IPS = config["excluded_ips"]
                
            # Ottieni l'uptime corrente per il controllo di riavvio
            try:
                uptime = get_uptime()
            except:
                uptime = 0
                
            # Controllo della connessione internet ogni 60 secondi
            try:
                if (time.time() % 60) < 1:  # Esegui ogni 60 secondi (quando il resto della divisione per 60 è < 1)
                    check_internet_connection()
            except Exception as e:
                print(f"Errore nel controllo della connessione internet: {e}")
                
            # Controllo accessi SSH ogni 30 secondi (per evitare troppi controlli)
            current_time = time.time()
            if (current_time - last_check_time) >= 30:
                print("\n--- Controllo accessi SSH ---")
                
                # Esegui il monitoraggio degli accessi SSH da auth.log
                # Verifica se le notifiche SSH sono abilitate nelle impostazioni degli alert (priorità più alta)
                alert_settings = config.get("alert_settings", {})
                ssh_settings = alert_settings.get("ssh", {})
                
                # Le impostazioni degli alert hanno precedenza sul flag globale
                ssh_alerts_enabled = ssh_settings.get("enabled", config.get("notify_ssh", True))
                
                if ssh_alerts_enabled:
                    try:
                        check_auth_log()
                    except Exception as e:
                        print(f"Errore durante check_auth_log: {e}")
                else:
                    print("Notifiche SSH disabilitate dalle impostazioni")
                    
                last_check_time = current_time
                print("--- Fine controllo ---\n")

            # Verifica se c'è stato un riavvio
            # Debug per rilevamento riavvii
            uptime_formatted = format_uptime(uptime)
            print(f"Debug riavvio - Uptime corrente: {uptime} ({uptime_formatted}), Uptime precedente: {last_uptime} ({format_uptime(last_uptime)})")
            
            # Verifica se c'è stato un riavvio (uptime corrente < uptime precedente)
            if uptime < last_uptime and last_uptime > 10:  # Previene falsi positivi con uptime molto bassi
                print("RIAVVIO RILEVATO! L'uptime è diminuito.")
                
                # Controlla le impostazioni delle notifiche di riavvio negli alert settings
                alert_settings = config.get("alert_settings", {})
                reboot_settings = alert_settings.get("reboot", {})
                
                # Debug impostazioni notifiche
                print(f"Debug impostazioni riavvio - Alert impostazioni: {reboot_settings}")
                print(f"Debug impostazioni riavvio - notify_reboot da config: {config.get('notify_reboot', True)}")
                
                # Le impostazioni degli alert hanno precedenza sul flag globale
                reboot_alerts_enabled = reboot_settings.get("enabled", config.get("notify_reboot", True))
                print(f"Debug riavvio - Notifiche abilitate: {reboot_alerts_enabled}")
                
                if reboot_alerts_enabled:
                    try:
                        local_ip = get_local_ip()
                        hostname = socket.gethostname()
                        message = f"🔄 *Server riavviato*\n\nHostname: *{hostname}* ({local_ip})\nUptime attuale: {uptime_formatted}"
                        
                        # Uso alert_type="reboot" ed esplicito force=True per forzare l'invio
                        send_alert(message, alert_type="reboot", force=True)
                        print(f"Notifica di riavvio inviata. Uptime: {uptime_formatted}")
                    except Exception as e:
                        print(f"Errore durante l'invio della notifica di riavvio: {e}")
                else:
                    print("Riavvio rilevato ma notifiche disabilitate dalle impostazioni")
                    
            last_uptime = uptime
            
            time.sleep(10)
            
        except Exception as e:
            print(f"Errore nel monitor_loop: {e}")
            time.sleep(10)  # In caso di errore, aspetta comunque prima di riprovare

# ----------------------------------------
# Rotte Flask
# ----------------------------------------

@app.route("/test_bot_connection", methods=["POST"])
def test_bot_connection():
    """Testa la connessione con il bot Telegram inviando un messaggio di prova direttamente via API"""
    try:
        # Carica la configurazione per avere i token più aggiornati
        config = load_config()
        bot_token = config.get("bot_token", "")
        chat_id = config.get("chat_id", "")
        
        if not bot_token or not chat_id:
            return {"success": False, "error": "Bot token o Chat ID non configurati"}, 400
            
        # Usa curl per inviare direttamente il messaggio alla API di Telegram
        import subprocess
        curl_cmd = [
            "curl", "-s", 
            "-d", f"chat_id={chat_id}&text=Test messaggio da Server Monitor&parse_mode=Markdown", 
            f"https://api.telegram.org/bot{bot_token}/sendMessage"
        ]
        
        result = subprocess.run(curl_cmd, capture_output=True, text=True)
        
        if "true" in result.stdout.lower():
            return {"success": True}, 200
        else:
            return {"success": False, "error": f"Errore API Telegram: {result.stdout}"}, 400
    except Exception as e:
        print(f"Errore nel test di connessione: {e}")
        return {"success": False, "error": str(e)}, 500

@app.route("/", methods=["GET", "POST"])
def index():
    """Rotta principale per la configurazione"""
    if request.method == "POST":
        # Elabora gli IP esclusi
        excluded_ips = []
        if "excluded_ips" in request.form and request.form["excluded_ips"].strip():
            excluded_ips = [ip.strip() for ip in request.form["excluded_ips"].split(",")]
        
        # Recupera il valore del numero di processi da visualizzare
        top_processes = 5  # Valore predefinito
        if "top_processes" in request.form:
            try:
                top_processes = int(request.form["top_processes"])
                # Limita il valore tra 1 e 20
                top_processes = max(1, min(20, top_processes))
            except ValueError:
                pass
        
        # Elabora i mount points con le rispettive soglie e impostazioni di notifica
        mount_points = []
        mount_paths = request.form.getlist("mount_points[]")
        mount_thresholds = request.form.getlist("mount_thresholds[]")
        
        for i in range(len(mount_paths)):
            path = mount_paths[i].strip()
            if path:  # Consideriamo solo i campi non vuoti
                mount_point = {"path": path}
                
                # Elabora soglia
                try:
                    threshold = int(mount_thresholds[i]) if i < len(mount_thresholds) else 90
                    threshold = max(1, min(100, threshold))  # Limita tra 1 e 100
                except (ValueError, IndexError):
                    threshold = 90  # Valore predefinito
                mount_point["threshold"] = threshold
                
                mount_points.append(mount_point)
                
        # Valori Telegram - mantieni i valori esistenti se i campi sono vuoti
        bot_token = request.form.get("bot_token", "").strip()
        chat_id = request.form.get("chat_id", "").strip()
        
        # Se il campo è vuoto o contiene solo asterischi, usa il valore esistente
        existing_bot_token = request.form.get("existing_bot_token", "").strip()
        if not bot_token or all(c == '•' for c in bot_token) or '•' in bot_token:
            bot_token = existing_bot_token
            
        existing_chat_id = request.form.get("existing_chat_id", "").strip()
        if not chat_id or all(c == '•' for c in chat_id) or '•' in chat_id:
            chat_id = existing_chat_id
        
        # Gestisci impostazioni per gli alert
        # Costruisci la configurazione degli alert
        alert_settings = {
            "ssh": {
                "enabled": "ssh" in request.form,  # Riutilizziamo il checkbox esistente
                "reminder_interval": 0,  # Non inviare reminder per SSH
                "notify_recovery": False
            },
            "internet": {
                "enabled": "internet_alert_enabled" in request.form,
                "reminder_interval": 0,  # Non supporta i reminder (impossibile inviare senza connessione)
                "notify_recovery": "internet_alert_enabled" in request.form  # Usa lo stesso valore di enabled (sempre uguali)
            }
        }

        new_config = {
            "excluded_ips": excluded_ips,
            "top_processes": top_processes,
            "mount_points": mount_points,
            "notify_ssh": "ssh" in request.form,
            "notify_reboot": True,  # Sempre abilitato, rimossa opzione disabilitazione
            "bot_token": bot_token,
            "chat_id": chat_id,
            "alert_settings": alert_settings
        }
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(new_config, f, indent=2)
            return redirect("/")
        except Exception as e:
            print(f"Errore nel salvataggio della configurazione: {e}")
            return f"Errore nel salvataggio della configurazione: {e}", 500

    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    except Exception as e:
        print(f"Errore nella lettura della configurazione: {e}")
        # Configurazione di default
        config = {
            "alert_settings": {
                "ssh": {
                    "enabled": True,
                    "reminder_interval": 0,  # No reminder
                    "notify_recovery": False
                },
                "internet": {
                    "enabled": True,
                    "reminder_interval": 0,  # No reminder
                    "notify_recovery": True
                }
            },
            "excluded_ips": ["127.0.0.1", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"],
            "top_processes": 5,
            "monitored_mount_points": [],
            "notify_ssh": True,
            "notify_reboot": True,
            "bot_token": "",
            "chat_id": ""
        }
        
    # Assicurati che tutti i campi necessari siano presenti
    if "excluded_ips" not in config:
        config["excluded_ips"] = ["127.0.0.1", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"]
    if "top_processes" not in config:
        config["top_processes"] = 5
    if "monitored_mount_points" not in config:
        config["monitored_mount_points"] = []
    if "bot_token" not in config:
        config["bot_token"] = ""
    if "chat_id" not in config:
        config["chat_id"] = ""
    
    # Get active tab from form submission if available
    active_tab = request.form.get('active_tab', 'tab-general')
    return render_template("index.html", config=config, active_tab=active_tab)

# ----------------------------------------
# Punto di ingresso principale
# ----------------------------------------

if __name__ == "__main__":
    # Verifica che il file di configurazione esista, altrimenti lo crea
    if not os.path.exists(CONFIG_FILE):
        print(f"File di configurazione {CONFIG_FILE} non trovato, ne creo uno nuovo...")
        default_config = {
            "excluded_ips": ["127.0.0.1", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"],
            "top_processes": 5,
            "mount_points": [],
            "notify_ssh": True,
            "notify_reboot": True,  # Sempre abilitato, rimossa opzione disabilitazione
            "bot_token": os.getenv("BOT_TOKEN", ""),
            "chat_id": os.getenv("CHAT_ID", "")
        }
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(default_config, f, indent=2)
        except Exception as e:
            print(f"Errore nella creazione del file di configurazione: {e}")
    
    # Carica la configurazione
    print("Caricamento configurazione...")
    config = load_config()
    print(f"Configurazione caricata: {CONFIG_FILE}")
    
    # Inizializza il bot Telegram
    init_bot()
    
    # Avvia il monitor loop in un thread separato
    Thread(target=monitor_loop, daemon=True).start()
    
    # Avvia l'applicazione Flask
    app.run(host="0.0.0.0", port=5000)