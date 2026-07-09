"""Easyjob Live-Client (OAuth2, Resource Owner Password Flow)."""
import urllib3

from easyjob_client import EasyjobClient as _OAuthClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class EjLiveClient:
    def __init__(self, base_url: str, username: str, password: str):
        self._client = _OAuthClient(base_url, username, password)

    def search(self, query: str, limit: int = 30) -> list[dict]:
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

    def addresses_search(self, q: str, limit: int = 12) -> list[dict]:
        """Sucht Adressen über V2 Grid oder V1 list."""
        # Versuch 1: V2 Grid — Boolean-Params als Integer (nicht Python True/False)
        try:
            data = self._client._get("/api.json/v2/masterdata/addresses/grid", {
                "SearchText":    q,
                "ShowAddresses": 1,
                "ShowContacts":  0,
                "ShowLeads":     0,
                "ShowDeactivated": 0,
            })
            if isinstance(data, dict) and "Data" in data and "Columns" in data:
                cols = [c["Name"] for c in data["Columns"]]
                rows = [dict(zip(cols, row)) for row in data["Data"]]
                out = []
                for r in rows:
                    name = r.get("comp") or r.get("name2") or ""
                    if r.get("id") and name:
                        out.append({"id": r["id"], "name": name})
                        if len(out) >= limit:
                            break
                if out:
                    return out
        except Exception:
            pass

        # Versuch 2: V1 list — Keys: Id, IdAddress, Company, FirstName, LastName
        try:
            data = self._client._get("/api.json/addresses/list", {"searchtext": q})
            if isinstance(data, list):
                out = []
                for r in data:
                    # IdAddress immer als Kunden-ID (auch für Kontakte)
                    rid  = r.get("IdAddress") or r.get("Id") or r.get("ID") or r.get("id") or 0
                    comp = r.get("Company") or ""
                    person = " ".join(filter(None, [r.get("FirstName"), r.get("LastName")]))
                    if comp and person:
                        name = f"{comp} — {person}"
                    elif comp:
                        name = comp
                    elif person:
                        name = person
                    else:
                        name = ""
                    if rid and name:
                        out.append({"id": rid, "name": name})
                        if len(out) >= limit:
                            break
                return out
        except Exception:
            pass

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

