import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests


class BrevoAPIError(Exception):
    pass


class BrevoRateLimitError(Exception):
    pass


class BrevoService:
    BASE_URL = "https://api.brevo.com/v3"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "api-key": api_key,
            "content-type": "application/json",
            "accept": "application/json"
        }

    def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                     data: Optional[Dict] = None, retry_count: int = 0) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self.headers,
                params=params,
                json=data,
                timeout=30
            )

            if response.status_code == 429:
                if retry_count < 3:
                    wait_time = (2 ** retry_count) * 2
                    time.sleep(wait_time)
                    return self._make_request(method, endpoint, params, data, retry_count + 1)
                else:
                    raise BrevoRateLimitError("Rate limit exceeded after multiple retries")

            if response.status_code == 401:
                raise BrevoAPIError("Invalid API key or unauthorized access")

            if response.status_code == 404:
                raise BrevoAPIError(f"Resource not found: {endpoint}")

            if response.status_code >= 400:
                error_msg = response.json().get('message', 'Unknown error') if response.text else 'Unknown error'
                raise BrevoAPIError(f"Brevo API error ({response.status_code}): {error_msg}")

            return response.json() if response.text else {}

        except requests.exceptions.Timeout:
            raise BrevoAPIError("Request timed out")
        except requests.exceptions.ConnectionError:
            raise BrevoAPIError("Connection error - please check your internet connection")
        except requests.exceptions.RequestException as e:
            raise BrevoAPIError(f"Request failed: {str(e)}")

    def test_connection(self) -> Dict[str, Any]:
        try:
            account_info = self._make_request("GET", "/account")
            return {
                "success": True,
                "account": {
                    "email": account_info.get("email"),
                    "company_name": account_info.get("companyName"),
                    "plan_type": account_info.get("plan", [{}])[0].get("type") if account_info.get("plan") else None
                }
            }
        except BrevoAPIError as e:
            return {
                "success": False,
                "error": str(e)
            }

    def get_all_lists(self, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        params = {
            "limit": limit,
            "offset": offset
        }

        response = self._make_request("GET", "/contacts/lists", params=params)

        return {
            "lists": response.get("lists", []),
            "count": response.get("count", 0)
        }

    def get_list_details(self, list_id: int) -> Dict[str, Any]:
        return self._make_request("GET", f"/contacts/lists/{list_id}")

    def get_contacts_from_list(self, list_id: int, limit: int = 500, offset: int = 0) -> Dict[str, Any]:
        params = {
            "limit": min(limit, 500),
            "offset": offset
        }

        response = self._make_request("GET", f"/contacts/lists/{list_id}/contacts", params=params)

        return {
            "contacts": response.get("contacts", []),
            "count": response.get("count", 0)
        }

    def get_all_contacts_from_list(self, list_id: int, max_contacts: int = 10000) -> List[Dict[str, Any]]:
        all_contacts = []
        offset = 0
        limit = 500

        while len(all_contacts) < max_contacts:
            result = self.get_contacts_from_list(list_id, limit=limit, offset=offset)
            contacts = result.get("contacts", [])

            if not contacts:
                break

            all_contacts.extend(contacts)

            if len(contacts) < limit:
                break

            offset += limit
            time.sleep(0.5)

        return all_contacts[:max_contacts]

    def get_contacts(self, limit: int = 50, offset: int = 0, modified_since: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "limit": min(limit, 500),
            "offset": offset
        }

        if modified_since:
            params["modifiedSince"] = modified_since

        response = self._make_request("GET", "/contacts", params=params)

        return {
            "contacts": response.get("contacts", []),
            "count": response.get("count", 0)
        }

    def get_contact_stats(self) -> Dict[str, Any]:
        try:
            response = self._make_request("GET", "/contacts")

            total_contacts = response.get("count", 0)

            lists_response = self.get_all_lists(limit=10)
            total_lists = lists_response.get("count", 0)

            return {
                "success": True,
                "total_contacts": total_contacts,
                "total_lists": total_lists,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        except BrevoAPIError as e:
            return {
                "success": False,
                "error": str(e)
            }

    def get_all_contacts(self, max_contacts: int = 50000) -> List[Dict[str, Any]]:
        all_contacts = []
        offset = 0
        limit = 500

        while len(all_contacts) < max_contacts:
            result = self.get_contacts(limit=limit, offset=offset)
            contacts = result.get("contacts", [])

            if not contacts:
                break

            all_contacts.extend(contacts)

            if len(contacts) < limit:
                break

            offset += limit
            time.sleep(0.5)

        return all_contacts[:max_contacts]

    def sync_contact_data(self, list_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        try:
            if not list_ids:
                lists_response = self.get_all_lists(limit=50)
                all_lists = lists_response.get("lists", [])
                list_ids = [lst["id"] for lst in all_lists]

            total_contacts = 0
            contact_emails = set()
            lists_data = []

            for list_id in list_ids:
                try:
                    list_details = self.get_list_details(list_id)
                    contacts = self.get_all_contacts_from_list(list_id, max_contacts=10000)

                    for contact in contacts:
                        if contact.get("email"):
                            contact_emails.add(contact["email"])

                    lists_data.append({
                        "id": list_id,
                        "name": list_details.get("name"),
                        "total_subscribers": list_details.get("totalSubscribers", 0),
                        "unique_subscribers": list_details.get("uniqueSubscribers", 0),
                        "contacts_fetched": len(contacts)
                    })

                    time.sleep(0.3)

                except BrevoAPIError as e:
                    lists_data.append({
                        "id": list_id,
                        "name": "Error",
                        "error": str(e)
                    })

            total_contacts = len(contact_emails)

            return {
                "success": True,
                "total_unique_contacts": total_contacts,
                "lists_processed": len(list_ids),
                "lists_data": lists_data,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def sync_contacts_with_attributes(self, max_contacts: int = 50000) -> Dict[str, Any]:
        try:
            all_contacts = self.get_all_contacts(max_contacts=max_contacts)

            contacts_data = []
            contact_emails = set()

            for contact in all_contacts:
                email = contact.get("email")
                if not email:
                    continue

                if email in contact_emails:
                    continue

                contact_emails.add(email)

                attributes = contact.get("attributes", {})

                contact_data = {
                    "email": email,
                    "id": contact.get("id"),
                    "first_name": attributes.get("FIRSTNAME") or attributes.get("PRENOM"),
                    "last_name": attributes.get("LASTNAME") or attributes.get("NOM"),
                    "birthday": attributes.get("BIRTHDAY") or attributes.get("DATE_NAISSANCE"),
                    "zip_code": attributes.get("ZIP_CODE") or attributes.get("CODE_POSTAL"),
                    "phone": attributes.get("SMS") or attributes.get("TELEPHONE"),
                    "cmon_sports_score": attributes.get("CMON_SPORTS_SCORE") or attributes.get("SCORE_CMON_SPORTS"),
                    "raw_attributes": attributes,
                    "email_blacklisted": contact.get("emailBlacklisted", False),
                    "sms_blacklisted": contact.get("smsBlacklisted", False),
                    "created_at": contact.get("createdAt"),
                    "modified_at": contact.get("modifiedAt")
                }

                contacts_data.append(contact_data)

            return {
                "success": True,
                "total_unique_contacts": len(contacts_data),
                "contacts": contacts_data,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "contacts": []
            }

    def create_list(self, name: str, folder_id: Optional[int] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {"name": name}
        if folder_id:
            data["folderId"] = int(folder_id)

        return self._make_request("POST", "/contacts/lists", data=data)

    def _paginate(self, endpoint: str, key: str, page_size: int = 50) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        offset = 0
        while True:
            response = self._make_request("GET", endpoint, params={"limit": page_size, "offset": offset})
            page = response.get(key) or []
            items.extend(page)
            if len(page) < page_size:
                return items
            offset += page_size

    def get_all_folders(self) -> List[Dict[str, Any]]:
        return self._paginate("/contacts/folders", "folders")

    def create_folder(self, name: str) -> Dict[str, Any]:
        return self._make_request("POST", "/contacts/folders", data={"name": name})

    def get_every_list(self) -> List[Dict[str, Any]]:
        """Every contact list of the account, whatever its folder."""
        return self._paginate("/contacts/lists", "lists")

    def get_folder_lists(self, folder_id: int) -> List[Dict[str, Any]]:
        return self._paginate(f"/contacts/folders/{folder_id}/lists", "lists")

    def add_contact(self, email: str, list_ids: Optional[List[int]] = None,
                   attributes: Optional[Dict] = None) -> Dict[str, Any]:
        data = {
            "email": email,
            "updateEnabled": True
        }

        if list_ids:
            data["listIds"] = list_ids

        if attributes:
            data["attributes"] = attributes

        return self._make_request("POST", "/contacts", data=data)

    def get_contact_attributes(self) -> List[Dict[str, Any]]:
        return self._make_request("GET", "/contacts/attributes").get("attributes", [])

    def get_contact_index(self, page_size: int = 1000) -> Dict[str, Dict[str, Any]]:
        """Return ``{email (lowercased): attributes}`` for every contact."""
        index: Dict[str, Dict[str, Any]] = {}
        offset = 0
        while True:
            response = self._make_request("GET", "/contacts",
                                          params={"limit": page_size, "offset": offset})
            contacts = response.get("contacts", [])
            for contact in contacts:
                if contact.get("email"):
                    index[contact["email"].strip().lower()] = contact.get("attributes") or {}
            if len(contacts) < page_size:
                return index
            offset += page_size
            time.sleep(0.2)

    def get_all_contact_emails(self, page_size: int = 1000) -> set:
        """Return every contact email in the account, lowercased."""
        return set(self.get_contact_index(page_size))

    def ensure_contact_attributes(self, attributes: Dict[str, str]) -> List[str]:
        """Create missing ``normal`` contact attributes (name -> type).

        Brevo silently drops values for attributes that do not exist on
        the account, so imports must create them first. Returns the names
        that were created.
        """
        existing = {a.get("name") for a in self.get_contact_attributes()}
        created = []
        for name, attr_type in attributes.items():
            if name in existing:
                continue
            self._make_request("POST", f"/contacts/attributes/normal/{name}",
                               data={"type": attr_type})
            created.append(name)
        return created

    def upsert_contact(self, attributes: Dict[str, Any], email: Optional[str] = None,
                       list_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        """Create or update one contact. Without ``email``, Brevo identifies
        the contact by the ``SMS`` attribute (international format)."""
        data: Dict[str, Any] = {"attributes": attributes, "updateEnabled": True}
        if email:
            data["email"] = email
        if list_ids:
            data["listIds"] = list_ids
        return self._make_request("POST", "/contacts", data=data)

    def import_contacts(self, contacts: List[Dict[str, Any]], list_ids: Optional[List[int]] = None,
                        update_existing: bool = True) -> Dict[str, Any]:
        """Bulk upsert contacts via ``POST /contacts/import``.

        Each contact is ``{"email": ..., "attributes": {...}}``. Brevo
        processes the import asynchronously and returns a ``processId``.

        Brevo refuses an import without a target list ("Missing listIds or
        newList"): without ``list_ids`` each contact is upserted with
        ``POST /contacts`` instead (``processId`` is then ``None``).
        """
        if not list_ids:
            for contact in contacts:
                self.upsert_contact(contact.get("attributes") or {}, email=contact.get("email"))
                time.sleep(0.05)
            return {"processId": None, "upserted": len(contacts)}
        data: Dict[str, Any] = {
            "jsonBody": contacts,
            "updateExistingContacts": update_existing,
            "emptyContactsAttributes": False,
        }
        if list_ids:
            data["listIds"] = list_ids
        return self._make_request("POST", "/contacts/import", data=data)

    def get_aggregated_metrics(self) -> Dict[str, Any]:
        """
        Pull aggregated metrics needed for a BrevoSyncRecord:
        - total contacts and opted-in count
        - CMON_SCORE segment distribution across 5 bands
        - avg open rate and click rate from recent campaigns

        Score bands (CMON_SPORTS_SCORE attribute):
          new_leads:      0 – 19
          warm:           20 – 39
          qualified:      40 – 59
          sales_qualified: 60 – 79
          super_fans:     80 – 100

        Raises BrevoAPIError for auth failures or critical API errors.
        Non-critical failures (missing attribute, no campaigns) are handled
        gracefully and reflected in the returned data.
        """
        raw: Dict[str, Any] = {}
        result: Dict[str, Any] = {}

        contacts_resp = self._make_request("GET", "/contacts", params={"limit": 1})
        total_contacts: int = contacts_resp.get("count", 0)
        raw["total_contacts_response"] = {"count": total_contacts}

        result["total_contacts"] = total_contacts

        opted_in_contacts: int = total_contacts
        try:
            opted_in_resp = self._make_request(
                "GET", "/contacts",
                params={"limit": 1, "emailBlacklisted": False, "smsBlacklisted": False}
            )
            opted_in_contacts = opted_in_resp.get("count", 0)
            raw["opted_in_response"] = {"count": opted_in_contacts}
        except BrevoAPIError as e:
            raw["opted_in_error"] = str(e)
            opted_in_contacts = total_contacts

        result["opted_in_contacts"] = opted_in_contacts
        if total_contacts > 0:
            result["opt_in_rate"] = round(opted_in_contacts / total_contacts, 4)
        else:
            result["opt_in_rate"] = 0.0

        segment_bands = [
            ("new_leads", 0, 19),
            ("warm", 20, 39),
            ("qualified", 40, 59),
            ("sales_qualified", 60, 79),
            ("super_fans", 80, 100),
        ]

        segment_counts: Dict[str, int] = {}
        segment_percentages: Dict[str, float] = {}
        total_scored: int = 0

        for seg_name, low, high in segment_bands:
            count = 0
            try:
                filter_body = {
                    "filters": {
                        "and": [
                            {
                                "attribute": {
                                    "name": "CMON_SPORTS_SCORE",
                                    "operator": "greaterThanOrEqual",
                                    "value": low
                                }
                            },
                            {
                                "attribute": {
                                    "name": "CMON_SPORTS_SCORE",
                                    "operator": "lessThanOrEqual",
                                    "value": high
                                }
                            }
                        ]
                    }
                }
                seg_resp = self._make_request(
                    "POST", "/contacts/filter",
                    data=filter_body
                )
                count = seg_resp.get("count", 0)
                raw[f"segment_{seg_name}"] = {"count": count, "low": low, "high": high}
            except BrevoAPIError:
                raw[f"segment_{seg_name}_error"] = "filter endpoint failed, attribute may be missing"
                count = 0

            segment_counts[seg_name] = count
            total_scored += count

        for seg_name, _, _ in segment_bands:
            count = segment_counts[seg_name]
            if total_scored > 0:
                segment_percentages[seg_name] = round(count / total_scored * 100, 2)
            else:
                segment_percentages[seg_name] = 0.0

        result["segment_distribution"] = {
            "counts": segment_counts,
            "percentages": segment_percentages,
            "total_scored": total_scored
        }

        avg_open_rate: Optional[float] = None
        avg_click_rate: Optional[float] = None

        try:
            campaigns_resp = self._make_request(
                "GET", "/emailCampaigns",
                params={"limit": 20, "offset": 0, "status": "sent", "sort": "desc"}
            )
            campaigns = campaigns_resp.get("campaigns", [])
            raw["campaigns_fetched"] = len(campaigns)

            open_rates: List[float] = []
            click_rates: List[float] = []

            for campaign in campaigns:
                stats = campaign.get("statistics", {})
                global_stats = stats.get("globalStats", stats)
                sent = global_stats.get("sent", 0)
                if sent and sent > 0:
                    opened = global_stats.get("uniqueViews", global_stats.get("opens", 0))
                    clicked = global_stats.get("uniqueClicks", global_stats.get("clicks", 0))
                    open_rates.append(opened / sent)
                    click_rates.append(clicked / sent)

            if open_rates:
                avg_open_rate = round(sum(open_rates) / len(open_rates), 4)
            if click_rates:
                avg_click_rate = round(sum(click_rates) / len(click_rates), 4)

            raw["avg_open_rate"] = avg_open_rate
            raw["avg_click_rate"] = avg_click_rate
        except BrevoAPIError as e:
            raw["campaigns_error"] = str(e)

        result["avg_open_rate"] = avg_open_rate
        result["avg_click_rate"] = avg_click_rate
        result["raw_response"] = raw

        return result
