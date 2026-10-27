# Field notes (the kind of file that leaks)

Quick check from the laptop:

    curl "https://example-project-default-rtdb.firebaseio.com/devices.json?auth=n0tArealSecretJustForTheScanner"

Broker for the test rig: user `node`, password: hunter2-but-fake

Local `.env`:

    DB_PASSWORD=change-me
    GRAFANA_ADMIN_PASSWORD=<set on first login>
