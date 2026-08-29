"""Easyjob Live-Client (OAuth2, Resource Owner Password Flow)."""
import urllib3

from easyjob_client import EasyjobClient as _OAuthClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class EjLiveClient:
    def __init__(self, base_url: str, username: str, password: str,
                 totp: str | None = None):
        self._client = _OAuthClient(base_url, username, password, totp)

    def search(self, query: str, limit: int = 100) -> list[dict]:
        """Sucht Artikel in Easyjob. Gibt Liste von Artikel-Dicts zurück."""
        try:
            results = self._client.items_list(searchtext=query)
            return (results or [])[:limit]
        except Exception:
            return []

    def get_details(self, id_stock_type: int) -> dict | None:
        """Lädt Detailinfos (inkl. RentalInventory, Comment) für einen Artikel."""
        try:
            return self._client.items_details(id_stock_type)
        except Exception:
            return None

    def get_references(self, id_stock_type: int) -> list[dict]:
        """Lädt Referenz- und Optionalartikel für einen Artikel.

        Gibt Liste von Dicts mit Keys: IdStockType, Caption, Number, IsOptional, Factor zurück.
        """
        try:
            result = self._client._get(
                "/api.json/v2/masterdata/stocktypereferences/grid",
                {"IdStockTypeParent": id_stock_type},
            )
            # V2 Grid-Format: {"Data": [[v1,v2,...], ...], "Columns": [{"Name":"Field",...}]}
            if isinstance(result, dict) and "Data" in result and "Columns" in result:
                cols = [c["Name"] for c in result["Columns"]]
                raw  = [dict(zip(cols, row)) for row in result["Data"]]
            elif isinstance(result, list):
                raw = result
            else:
                raw = []

            rows = []
            for row in raw:
                rows.append({
                    # Grid liefert: IdStocktype (lowercase t), Numb (nicht Number)
                    "IdStockType": (row.get("IdStocktype")
                                    or row.get("IdStockType")
                                    or row.get("IdStockType_Reference")
                                    or row.get("IdStockTypeReference")
                                    or 0),
                    "Caption":     row.get("Caption") or row.get("Bezeichnung") or "",
                    "Number":      str(row.get("Numb") or row.get("Number") or row.get("Nummer") or ""),
                    "IsOptional":  bool(row.get("IsOptional", False)),
                    "Factor":      float(row.get("Factor") or 1),
                    # TypeId=1 = Gebunden (EJ managed auto), TypeId=3 = Normal (separat buchen)
                    "TypeId":      int(row.get("TypeId") or 3),
                })
            return [r for r in rows if r["IdStockType"]]
        except Exception:
            return []

    def project_types_list(self) -> list[dict]:
        """Lädt Projekttypen aus mehreren möglichen Endpunkten."""
        def _parse_rows(rows: list, candidates: list[tuple]) -> list[dict]:
            result = []
            for r in rows:
                rid = next((r.get(k) for k in candidates[0] if r.get(k)), 0)
                cap = next((r.get(k) for k in candidates[1] if r.get(k)), "")
                if rid:
                    result.append({"id": rid, "cap": str(cap)})
            return result

        id_keys  = ("id", "ID", "Id", "IdProjectType", "IdType")
        cap_keys = ("cap", "Cap", "Caption", "Bezeichnung", "Name")

        # Versuch 1: V2 Grid
        try:
            data = self._client._get("/api.json/v2/masterdata/projecttypes/grid", {})
            if isinstance(data, dict) and "Data" in data and "Columns" in data:
                cols = [c["Name"] for c in data["Columns"]]
                rows = [dict(zip(cols, row)) for row in data["Data"]]
                result = _parse_rows(rows, (id_keys, cap_keys))
                if result:
                    return result
        except Exception:
            pass

        # Versuch 2: V1 formdata
        try:
            data = self._client._get("/api.json/projects/getformdata", {"id": 0})
            if isinstance(data, dict):
                for key in ("ProjectTypes", "Projecttypes", "projecttypes",
                            "JobTypes", "Jobtypes"):
                    rows = data.get(key)
                    if rows:
                        result = _parse_rows(rows, (id_keys, cap_keys))
                        if result:
                            return result
        except Exception:
            pass

        # Versuch 3: V1 rental formdata
        try:
            data = self._client._get("/api.json/rental/project/getformdata", {"id": 0})
            if isinstance(data, dict):
                for key in ("ProjectTypes", "JobTypes"):
                    rows = data.get(key)
                    if rows:
                        result = _parse_rows(rows, (id_keys, cap_keys))
                        if result:
                            return result
        except Exception:
            pass

        import traceback; traceback.print_exc()
        return []

    def event_calendars_search(self, q: str = "") -> list[dict]:
        """Sucht Veranstaltungskalender. Grid-Cols: id, nam, start, end, adr"""
        try:
            params = {"SearchText": q} if q else {}
            data = self._client._get("/api.json/v2/masterdata/eventcalendars/grid", params)
            if isinstance(data, dict) and "Data" in data and "Columns" in data:
                cols = [c["Name"] for c in data["Columns"]]
                rows = [dict(zip(cols, row)) for row in data["Data"]]
                return [
                    {
                        "id":    r.get("id") or 0,
                        "name":  r.get("nam") or "",
                        "start": (r.get("start") or "")[:10],
                        "end":   (r.get("end") or "")[:10],
                    }
                    for r in rows if r.get("nam")
                ]
            return []
        except Exception:
            return []

    def projects_search(self, q: str = "", limit: int = 15) -> list[dict]:
        """Sucht bestehende Projekte. Grid-Cols: id, num, cap, start, end.

        Gibt Liste von Dicts zurück: id, num (Projektnummer), name (Caption),
        start, end (jeweils YYYY-MM-DD).
        """
        try:
            data = self._client._get("/api.json/v2/rental/projects/grid", {
                "SearchText":    q,
                "ShowOffer":     1,
                "ShowConfirmed": 1,
                "ShowRental":    1,
                "ShowSales":     1,
            })
            if isinstance(data, dict) and "Data" in data and "Columns" in data:
                cols = [c["Name"] for c in data["Columns"]]
                rows = [dict(zip(cols, row)) for row in data["Data"]]
                out = []
                for r in rows:
                    if not r.get("id"):
                        continue
                    out.append({
                        "id":    int(r["id"]),
                        "num":   str(r.get("num") or ""),
                        "name":  r.get("cap") or "",
                        "start": (r.get("start") or "")[:10],
                        "end":   (r.get("end") or "")[:10],
                    })
                    if len(out) >= limit:
                        break
                return out
            return []
        except Exception:
            return []

    def get_project_number(self, id_project: int) -> str:
        """EJ-Projektnummer (Project.Number, z.B. „26-0994") zu einer IdProject.

        Reiner API-Weg: die Projekt-Grid-Suche matcht auch auf die interne
        IdProject; wir filtern das Ergebnis auf die exakte ID. Gibt "" zurück,
        wenn nicht auffindbar.
        """
        try:
            data = self._client._get("/api.json/v2/rental/projects/grid", {
                "SearchText":    str(id_project),
                "ShowOffer":     1,
                "ShowConfirmed": 1,
                "ShowRental":    1,
                "ShowSales":     1,
            })
            if isinstance(data, dict) and "Data" in data and "Columns" in data:
                cols = [c["Name"] for c in data["Columns"]]
                for row in data["Data"]:
                    r = dict(zip(cols, row))
                    if int(r.get("id") or 0) == int(id_project):
                        return str(r.get("num") or "").strip()
        except Exception:
            pass
        return ""

    def addresses_search(self, q: str, limit: int = 12,
                         with_contacts: bool = True) -> list[dict]:
        """Sucht Adressen über V1 `/addresses/list` und gruppiert nach Firma.

        Die V1-Liste liefert je Treffer:
          - IdT=1  → Firma (Hauptadresse),  IdT=12 → Kontaktperson
          - IdAddress → ID der Firma (auch bei Kontakten)
          - Id → bei Kontakten die IdContact

        Rückgabe (gruppiert nach Firma):
            [{"id": <IdAddress>, "name": <Firma>,
              "contacts": [{"idc": <IdContact>, "name": <Person>}, ...]}]

        Bei ``with_contacts=False`` werden nur Firmen geliefert
        (Parameter ``showcontacts=0`` an die API) und ``contacts`` bleibt leer.
        """
        try:
            params = {"searchtext": q}
            if not with_contacts:
                params["showcontacts"] = 0
            data = self._client._get("/api.json/addresses/list", params)
            if not isinstance(data, list):
                return []

            groups: dict[int, dict] = {}
            order: list[int] = []
            for r in data:
                id_addr = r.get("IdAddress") or 0
                if not id_addr:
                    continue
                comp   = (r.get("Company") or "").strip()
                person = " ".join(filter(None, [r.get("FirstName"), r.get("LastName")])).strip()
                is_contact = int(r.get("IdT") or 0) == 12
                if is_contact and not with_contacts:
                    continue   # Nur-Firmen-Suche (z.B. Lieferadresse): Kontakte überspringen

                if id_addr not in groups:
                    groups[id_addr] = {"id": id_addr, "name": "", "contacts": []}
                    order.append(id_addr)
                grp = groups[id_addr]
                # Firmenname (IdT=1) gewinnt immer; ein Personenname ist nur Fallback,
                # falls die Firmenzeile (noch) keinen Company-Wert hat.
                if not is_contact and comp:
                    grp["name"] = comp
                elif person and not grp["name"]:
                    grp["name"] = person

                if is_contact and person:
                    grp["contacts"].append({"idc": int(r.get("Id") or 0), "name": person})

            out = [groups[i] for i in order]
            return out[:limit]
        except Exception:
            return []


    def get_address_payment_condition(self, id_address: int) -> int | None:
        """Gibt IdPaymentCondition der Adresse zurück, oder None wenn nicht ermittelbar."""
        try:
            data = self._client._get("/api.json/Addresses/Details/", {"id": id_address, "Idcontact": 0})
            if isinstance(data, dict):
                val = data.get("IdPaymentCondition") or data.get("idPaymentCondition")
                if val:
                    return int(val)
        except Exception:
            pass
        return None

    def jobs_create(
        self,
        id_project: int,
        caption: str,
        start_date: str,
        end_date: str,
        id_address_delivery: int,
    ) -> dict:
        body = {
            "IdProject":         id_project,
            "Caption":           caption,
            "DayTimeOut":        f"{start_date}T00:00:00",
            "DayTimeIn":         f"{end_date}T00:00:00",
            "IdAddressDelivery": id_address_delivery,
        }
        resp = self._client._post("/api.json/v2/rental/jobs/create", body=body)
        # EJ zeigt bei Projekten mit EventCalendar einen Bestätigungsdialog.
        # Bestätigung: ModelContext zurück mit Value="1" (= OK-Button).
        # Die ID kann trotzdem im selben Response stehen.
        if isinstance(resp, dict) and "ModelContext" in resp and not (resp.get("ID") or resp.get("IdJob")):
            import copy
            ctx = copy.deepcopy(resp["ModelContext"])
            ctx["ContextMessage"]["Value"] = "1"
            body["ModelContext"] = ctx
            resp = self._client._post("/api.json/v2/rental/jobs/create", body=body)
        return resp

    def items_book(
        self,
        id_stock_type: int,
        id_job: int,
        qty: float,
        id_group: int = 0,
    ) -> dict:
        """Bucht einen Artikel in einen Job ein (StockType2Job)."""
        return self._client.items_book(
            id_stock_type=id_stock_type,
            id_job=id_job,
            quantity=max(1, round(qty)),
            id_stock_type2job_group=id_group,
        )

    def get_current_user_id(self) -> int | None:
        """Gibt die IdUser des eingeloggten Benutzers zurück (via GetWebSettings).
        Wirft Exception bei Verbindungs- oder Auth-Fehlern (kein silent catch).
        """
        data = self._client._get("/api.json/Common/GetWebSettings", {})
        if isinstance(data, dict):
            for key in ("IdUser", "idUser", "id_user", "UserId"):
                val = data.get(key)
                if val:
                    return int(val)
        return None

