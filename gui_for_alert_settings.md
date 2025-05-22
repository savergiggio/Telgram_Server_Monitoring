# Implementazione delle Impostazioni di Alert per la GUI

Ecco una proposta di implementazione per la gestione degli alert di sistema con opzioni configurabili tramite GUI.

## Struttura delle Funzioni Implementate

1. **Sistema di Gestione Alert**
   - `load_active_alerts()` - Carica lo stato degli alert attivi
   - `save_active_alerts()` - Salva lo stato degli alert attivi
   - `send_alert()` - Nuova funzione che gestisce notifica iniziale, reminder e storia
   - `send_recovery_alert()` - Gestisce le notifiche di ripristino

2. **Configurazione Alert**
   - Nuova struttura di configurazione in `config.json`
   - Sezione `alert_settings` con impostazioni per ogni tipo di alert

## Schema di Configurazione

```json
"alert_settings": {
    "cpu": {
        "enabled": true,
        "reminder_interval": 3600,  // Secondi (1 ora)
        "notify_recovery": true
    },
    "ram": {
        "enabled": true,
        "reminder_interval": 3600,
        "notify_recovery": true
    },
    "disk": {
        "enabled": true,
        "reminder_interval": 7200,  // Secondi (2 ore)
        "notify_recovery": true
    },
    "temperature": {
        "enabled": true,
        "reminder_interval": 1800,  // Secondi (30 minuti)
        "notify_recovery": true
    },
    "ssh": {
        "enabled": true,
        "reminder_interval": 0,      // 0 = No reminder
        "notify_recovery": false
    }
}
```

## Funzionalità Implementate

1. **Notifica Iniziale**
   - Quando un problema viene rilevato viene inviata una notifica
   - Lo stato dell'alert viene salvato con timestamp

2. **Reminder Periodici**
   - Configurabili per tipo di alert (in ore o secondi)
   - Possono essere disabilitati impostando a 0
   - Si evita l'invio se il tempo dall'ultimo reminder è inferiore a quello configurato

3. **Notifiche di Ripristino**
   - Inviate quando un problema rientra nei limiti
   - Indicano la durata del problema
   - Possono essere disabilitate per tipo di alert

4. **Disabilitazione Alert**
   - Ogni tipo di alert può essere disabilitato completamente

## Interfaccia Utente

Per implementare l'interfaccia utente, è necessario:

1. Aggiungere una nuova sezione alla pagina di configurazione
2. Creare controlli per ogni tipo di alert con:
   - Checkbox per abilitare/disabilitare
   - Input per l'intervallo di reminder (con selezione ore/giorni)
   - Checkbox per notifiche di ripristino

## Esempio di Modifica alla GUI

```html
<div class="card mt-4">
  <div class="card-header">
    <h5>Impostazioni Notifiche</h5>
  </div>
  <div class="card-body">
    <div class="row">
      <div class="col-md-6">
        <h6>CPU</h6>
        <div class="form-check mb-2">
          <input class="form-check-input" type="checkbox" id="cpu_alert_enabled" name="cpu_alert_enabled">
          <label class="form-check-label" for="cpu_alert_enabled">Abilita notifiche</label>
        </div>
        <div class="form-group row">
          <label class="col-sm-6 col-form-label">Reminder ogni:</label>
          <div class="col-sm-3">
            <input type="number" class="form-control" id="cpu_reminder_interval" name="cpu_reminder_interval" min="0">
          </div>
          <div class="col-sm-3">
            <select class="form-control" id="cpu_reminder_unit">
              <option value="3600">Ore</option>
              <option value="86400">Giorni</option>
            </select>
          </div>
        </div>
        <div class="form-check">
          <input class="form-check-input" type="checkbox" id="cpu_notify_recovery" name="cpu_notify_recovery">
          <label class="form-check-label" for="cpu_notify_recovery">Notifica di ripristino</label>
        </div>
      </div>
      
      <!-- Ripeti per RAM, Disk, Temperature, SSH -->
    </div>
  </div>
</div>
```

## Conclusione

Questo sistema consente una gestione intelligente delle notifiche, riducendo lo spam ed evidenziando le informazioni importanti. Le impostazioni configurabili permettono agli utenti di adattare il comportamento alle proprie esigenze.

Tutte le modifiche mantengono la retrocompatibilità con il sistema esistente, aggiungendo solo nuove funzionalità senza compromettere quelle esistenti.