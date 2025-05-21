# Server Monitor

![Server Monitor Logo](https://img.shields.io/badge/Server-Monitor-blue)
![Docker](https://img.shields.io/badge/Docker-Ready-blue)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue)

Server Monitor è un'applicazione di monitoraggio server con notifiche Telegram, perfetta per tenere sotto controllo i tuoi server remoti. Ricevi avvisi tempestivi su problemi critici e controlla lo stato del sistema attraverso un bot Telegram interattivo.

## Caratteristiche

### Monitoraggio Risorse

- **CPU**: Monitoraggio utilizzo e temperatura con avvisi personalizzabili
- **RAM**: Monitoraggio utilizzo memoria con soglie configurabili
- **Disco**: Monitoraggio spazio disco su multipli mount point
- **Rete**: Monitoraggio traffico di rete con statistiche di download/upload
- **Docker**: Gestione completa dei container Docker (lista, start, stop, restart)

### Sistema di Alert Intelligente

- **Notifiche configurabili**: Abilita/disabilita notifiche per ogni tipo di risorsa
- **Reminder periodici**: Configurabili per tipo di alert (in ore o secondi)
- **Notifiche di ripristino**: Avvisi quando un problema rientra nei parametri normali
- **Storico problemi**: Tracciamento della durata dei problemi

### Bot Telegram Interattivo

- **Dashboard risorse**: Visualizza lo stato attuale del server
- **Comandi di amministrazione**: Riavvio e spegnimento da remoto
- **Gestione Docker**: Gestisci i container direttamente da Telegram
- **Upload file**: Carica file sul server tramite Telegram

### Altre Funzionalità

- **Notifiche accessi SSH**: Ricevi avvisi quando qualcuno accede al server via SSH
- **Filtro IP**: Configura IP/reti da escludere dalle notifiche SSH
- **Interfaccia web**: Pannello di configurazione semplice e intuitivo
- **Containerizzato**: Facilmente distribuibile tramite Docker

## Architettura

Il sistema è composto da:

1. **Server Monitor**: Applicazione principale in Python
2. **Bot Telegram**: Interfaccia per interagire con il monitor
3. **Interfaccia Web**: Pannello di configurazione accessibile via browser
4. **Docker**: Containerizzazione per facile deployment

## Installazione

### Prerequisiti

- Docker e Docker Compose
- Un bot Telegram (creato tramite BotFather)
- Chat ID Telegram (ottenibile tramite @userinfobot)

### Passaggi

1. Clona il repository:
   ```bash
   git clone https://github.com/tuoutente/server-monitor.git
   cd server-monitor
   ```

2. Configura il file docker-compose.yml (adatta i volumi montati alle tue esigenze)

3. Avvia il container:
   ```bash
   docker-compose up -d
   ```

4. Accedi all'interfaccia web:
   ```
   http://server-ip:8181
   ```

5. Configura il bot Telegram inserendo il token e il chat ID

## Configurazione

La configurazione può essere effettuata attraverso l'interfaccia web accessibile alla porta 8181.

Impostazioni principali:

- **Soglie di allarme**: CPU, RAM, Disco, e Temperatura
- **Mount points**: Configurazione dei percorsi da monitorare
- **Impostazioni notifiche**: Personalizzazione per tipo di risorsa
- **Configurazione Telegram**: Token bot e Chat ID

## Sicurezza

Il server monitor funziona in un container Docker con accesso limitato al sistema host. Le funzionalità di amministrazione (riavvio, spegnimento) richiedono conferma esplicita.

## Licenza

Questo progetto è distribuito con licenza MIT. Vedi il file LICENSE per maggiori informazioni.

## Contribuire

I contributi sono ben accetti! Se vuoi migliorare Server Monitor, sentiti libero di creare una pull request o aprire una issue.

---

*Server Monitor - Monitoring semplice ed efficace per i tuoi server*
