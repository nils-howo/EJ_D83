"""
Easyjob WebApi v6 – Python Client
Auth: OAuth2 Password Flow (Bearer Token)
"""

import requests
from datetime import datetime, timedelta


class EasyjobClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires: datetime = datetime.min
        self._session = requests.Session()
        self._session.verify = False  # self-signed cert

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    # Pflicht-Header laut Protonic-Doku für volle API-Funktionalität (inkl. Refresh Token)
    _EJ_HEADER = {"ej-webapi-client": "ThirdParty"}

    def _fetch_token(self):
        resp = self._session.post(
            f"{self.base_url}/token",
            data={
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", **self._EJ_HEADER},
        )
        if not resp.ok:
            raise RuntimeError(
                f"Token-Fehler {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        self._token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        expires_in = int(data.get("expires_in", 3600))
        self._token_expires = datetime.now() + timedelta(seconds=expires_in - 30)

    def _auth_header(self) -> dict:
        if not self._token or datetime.now() >= self._token_expires:
            self._fetch_token()
        return {"Authorization": f"Bearer {self._token}", **self._EJ_HEADER}

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None, max_retries: int = 3) -> dict | list:
        for attempt in range(1, max_retries + 1):
            resp = self._session.get(
                f"{self.base_url}{path}",
                headers=self._auth_header(),
                params={k: v for k, v in (params or {}).items() if v is not None},
            )
            if resp.status_code == 200:
                return resp.json()
            if attempt < max_retries:
                import time; time.sleep(1)
        resp.raise_for_status()

    def _post(self, path: str, body: dict = None, params: dict = None, max_retries: int = 3) -> dict | list:
        import time
        last_resp = None
        for attempt in range(1, max_retries + 1):
            resp = self._session.post(
                f"{self.base_url}{path}",
                headers=self._auth_header(),
                json=body,
                params={k: v for k, v in (params or {}).items() if v is not None},
            )
            last_resp = resp
            if resp.status_code == 200 and resp.text:
                data = resp.json()
                # Wenn die API ein Success-Feld liefert, nur bei True aufhoeren
                if "Success" not in data or data["Success"] is True:
                    return data
                # Success: false → nochmal versuchen
                if attempt < max_retries:
                    time.sleep(1)
                    continue
                raise RuntimeError(f"API Error: {data.get('Message') or data.get('ErrorMessages')}")
            # 500 mit leerem Body: Server-Bug nach erfolgreichem Schreiben → nicht nochmal senden
            if resp.status_code == 500 and not resp.text:
                raise RuntimeError(
                    f"HTTP 500 (leere Antwort) auf {path} – Datensatz wurde moeglicherweise "
                    f"trotzdem angelegt. Bitte manuell pruefen."
                )
            if attempt < max_retries:
                time.sleep(1)
        last_resp.raise_for_status()

    # ------------------------------------------------------------------
    # Activities
    # ------------------------------------------------------------------

    def activities_list(self, style: str = "List") -> list:
        """Aktivitaeten des aktuellen Benutzers."""
        return self._get("/api.json/activities/list", {"style": style})

    def activities_list2object(self, id: int, style: str = "List") -> list:
        """Aktivitaeten zu einem bestimmten Objekt."""
        return self._get("/api.json/activities/list2object", {"id": id, "style": style})

    # ------------------------------------------------------------------
    # Items (Artikel)
    # ------------------------------------------------------------------

    def items_categorylist(self) -> list:
        """Alle Artikelkategorien."""
        return self._get("/api.json/items/categorylist")

    def items_list(
        self,
        searchtext: str = None,
        type: str = "View",
        style: str = "List",
        id_category: int = None,
        id_category_parent: int = None,
        id_user_filter: int = None,
    ) -> list:
        """Artikelliste mit optionaler Suche und Filterung."""
        return self._get("/api.json/items/list", {
            "searchtext": searchtext,
            "type": type,
            "style": style,
            "IdCategory": id_category,
            "IdCategoryParent": id_category_parent,
            "IdUserFilter": id_user_filter,
        })

    def items_details(self, id: int, additionalfields: str = None) -> dict:
        """Detailinfos zu einem Artikel."""
        return self._get("/api.json/items/details", {
            "id": id,
            "additionalfields": additionalfields,
        })

    def items_add_group(self, id_job: int, caption: str) -> dict:
        """Neue Gruppe (StockType2JobGroup) in einem Job anlegen.

        Gibt {"Success": true, "ID": <IdStockType2JobGroup>} zurück.
        Die Gruppe wird automatisch der ersten vorhandenen Hauptgruppe zugeordnet.
        """
        return self._post(
            "/api.json/Items/AddGroup/",
            params={"id": id_job, "caption": caption},
        )

    def items_book(
        self,
        id_stock_type: int,
        id_job: int,
        quantity: int = 1,
        id_stock_type2job_group: int = None,
    ) -> dict:
        """Artikel (inkl. Stückliste) auf einen Job buchen.

        Gibt {"Success": true, "ID": <IdStockType2Job>} zurück.
        Bucht automatisch alle BOM-Positionen mit in dieselbe Gruppe.
        """
        return self._post("/api.json/items/book", body={
            "IdStockType":          id_stock_type,
            "IdJob":                id_job,
            "Quantity":             quantity,
            "IdStockType2JobGroup": id_stock_type2job_group or 0,
        })

    # ------------------------------------------------------------------
    # Addresses (Adressen)
    # ------------------------------------------------------------------

    def addresses_list(self, searchtext: str = None, style: str = "List") -> list:
        return self._get("/api.json/addresses/list", {
            "searchtext": searchtext,
            "style": style,
        })

    def addresses_details(self, id: int) -> dict:
        return self._get("/api.json/addresses/details", {"id": id})

    # ------------------------------------------------------------------
    # Rental - Jobs
    # ------------------------------------------------------------------

    def jobs_list(
        self,
        searchtext: str = None,
        start_date: str = None,
        end_date: str = None,
        style: str = "List",
    ) -> list:
        return self._get("/api.json/Jobs/List/", {
            "searchtext": searchtext or "",
            "startdate":  start_date,
            "enddate":    end_date,
            "style":      style,
        })

    def jobs_details(self, id: int) -> dict:
        return self._get("/api.json/v2/rental/jobs/details", {"id": id})

    # ------------------------------------------------------------------
    # Rental - Projects
    # ------------------------------------------------------------------

    def projects_grid(self, search: str = None, start_date: str = None, end_date: str = None) -> list:
        data = self._get("/api.json/v2/rental/projects/grid", {
            "SearchText": search,
            "StartDate":  start_date,
            "EndDate":    end_date,
        })
        cols = [c["Name"] for c in data["Columns"]]
        return [dict(zip(cols, row)) for row in data["Data"]]

    def projects_details(self, id: int) -> dict:
        return self._get("/api.json/v2/rental/projects/formdata", {"id": id})

    def projects_create(
        self,
        caption: str,
        start_date: str,
        end_date: str,
        id_address_customer: int,
        job_caption: str = "Job 1",
        id_user_arranger: int = 212,
        id_project_type: int = 9,
        id_priority: int = 2,
        id_payment_condition: int = 8,
        id_job_state: int = 2,
        id_job_service: int = 2,
        id_company: int = 1,
        id_address_delivery: int = None,
    ) -> dict:
        resp = self._post("/api.json/v2/rental/projects/create", body={
            "IdProject":          0,
            "Caption":            caption,
            "StartDate":          start_date,
            "EndDate":            end_date,
            "IdUser_Arranger":    id_user_arranger,
            "IdAddress_Customer": id_address_customer,
            "IdContact_Customer": 0,
            "IdAddressDelivery":  id_address_delivery or id_address_customer,
            "IdContactDelivery":  0,
            "IdProjectType":      id_project_type,
            "IdPriority":         id_priority,
            "IdPaymentCondition": id_payment_condition,
            "IdJobState":         id_job_state,
            "IdJobService":       id_job_service,
            "JobCaption":         job_caption,
            "IdStock":            1,
            "IdEventCalendar":    0,
            "Opportunity":        0,
            "IdCurrencyBase":     1,
            "IdCurrencyTarget":   1,
            "IdCostCenter":       0,
            "IdCompany":          id_company,
            "IdCompanyStructure": 0,
            "RefNumber":          "",
        })
        return resp

    # ------------------------------------------------------------------
    # Shortcuts / Datei-Anhänge  (V1)
    # ------------------------------------------------------------------

    # table-Parameter → IdShortCutObjectType in DB:
    # 1=Address(1)  2=Item(2)  3=Project(1)  16=Workshop(4)  23=PurchaseInvoice(8)
    # NICHT unterstützt via V1: 4=Job, 6=Invoice, 10=Device, 12=Contact
    # V2 /v2/main/shortcuts/upload unterstützt PurchaseInvoice (type=8 oder 23) NICHT

    def shortcuts_upload_base64(
        self,
        id_object: int,
        table: int,
        base64_content: str,
        filename: str,
        caption: str = None,
        set_default: bool = False,
    ) -> dict:
        """Datei als Base64-String an ein Objekt anhängen (V1).

        Gibt {"Success": true, "ID": <IdShortCut>} zurück.

        table-Werte (V1):
          1  = Address
          2  = Item (Artikel)
          3  = Project
         16  = Workshop
         23  = PurchaseInvoice (Eingangsbeleg) → speichert ObjectType=8 in DB
        """
        return self._post(
            "/api.json/Shortcuts/UploadBase64",
            params={
                "id":          id_object,
                "table":       table,
                "filename":    filename,
                "base64":      base64_content,
                "caption":     caption or filename,
                "setdefault":  str(set_default).lower(),
            },
        )

    def shortcuts_upload_purchase_invoice(
        self,
        id_purchase_invoice: int,
        base64_content: str,
        filename: str,
        caption: str = None,
    ) -> dict:
        """Datei an einen Eingangsbeleg (PurchaseInvoice) anhängen (V1, table=23)."""
        return self.shortcuts_upload_base64(
            id_object=id_purchase_invoice,
            table=23,
            base64_content=base64_content,
            filename=filename,
            caption=caption,
        )

    def shortcuts_list(self, id_object: int, table: int) -> list:
        """Datei-Anhänge eines Objekts auflisten (V1)."""
        return self._get("/api.json/Shortcuts/List2Object", {"id": id_object, "table": table})

    # ------------------------------------------------------------------
    # Barcode
    # ------------------------------------------------------------------

    def barcode_info(self, barcode: str) -> dict:
        return self._get("/api.json/Common/BarcodeSearch", {"id": barcode})


# ----------------------------------------------------------------------
# Beispiel-Nutzung
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import urllib3
    import json

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    client = EasyjobClient(
        base_url="http://EASYJOB-TEST:8008",
        username="DEIN_USERNAME",
        password="DEIN_PASSWORT",
    )

    print("=== Token holen ===")
    client._fetch_token()
    print(f"Token: {client._token[:40]}...")

    print("\n=== Artikelliste ===")
    items = client.items_list(searchtext="Kamera")
    print(json.dumps(items[:3], indent=2, ensure_ascii=False))

    print("\n=== Kategorien ===")
    cats = client.items_categorylist()
    print(json.dumps(cats[:5], indent=2, ensure_ascii=False))
