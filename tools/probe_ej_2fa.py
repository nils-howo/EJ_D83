"""
Prueft, unter welchem Feldnamen die easyJob WebApi den TOTP-Code am /token-
Endpunkt erwartet.

Hintergrund: Die OpenAPI-Spec dokumentiert an /token nur grant_type/username/
password. Der Live-Server lehnt 2FA-Benutzer mit {"error":"missing_totp"} ab,
nennt das Feld aber nicht. Der Client verwendet deshalb "totp" (umstellbar per
EJ_TOTP_FIELD) — dieses Skript bestaetigt oder widerlegt das.

    python tools/probe_ej_2fa.py <benutzer> <passwort> <aktueller-code>

Achtung: Ein TOTP-Code gilt nur ~30 s, und der Server zaehlt Fehlversuche mit
(WebApiUser.BadPasswordAttempts). Das Skript stoppt daher beim ersten Treffer
und probiert hoechstens eine Handvoll Varianten.
"""

import os
import sys

import requests

BASE = os.environ.get("EJ_BASE_URL", "http://EASYJOB-TEST:8008").rstrip("/")
HDR = {
    "Content-Type": "application/x-www-form-urlencoded",
    "ej-webapi-client": "ThirdParty",
}

# Nach Wahrscheinlichkeit sortiert — "missing_totp" legt "totp" nahe.
CANDIDATES = ("totp", "totp_code", "otp", "code", "two_factor_code")


def try_field(user: str, pwd: str, code: str, field: str) -> bool:
    data = {"grant_type": "password", "username": user, "password": pwd, field: code}
    try:
        r = requests.post(f"{BASE}/token", data=data, headers=HDR, timeout=(5, 30))
    except Exception as exc:
        print(f"  {field:<16} EXCEPTION {exc}")
        return False

    if r.ok:
        print(f"  {field:<16} {r.status_code}  <== AKZEPTIERT")
        return True

    body = r.text[:200].replace("\n", " ")
    print(f"  {field:<16} {r.status_code}  {body}")
    return False


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2

    user, pwd, code = sys.argv[1], sys.argv[2], sys.argv[3]
    print(f"Server: {BASE}\nNutzer: {user}\n" + "=" * 70)

    for field in CANDIDATES:
        if try_field(user, pwd, code, field):
            print(f"\nFeldname: {field}")
            if field != "totp":
                print(f"In die .env eintragen:  EJ_TOTP_FIELD={field}")
            else:
                print("Entspricht der Vorgabe — nichts zu tun.")
            return 0

    print("\nKeine Variante akzeptiert. Moeglich: Code abgelaufen (neu versuchen),"
          "\noder das Feld heisst anders — dann die Antworten oben auswerten.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
