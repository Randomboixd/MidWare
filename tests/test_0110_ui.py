"""Panel regressions: confirm dialogs built from template text, the credential
example on the keys page, the unmasked token on the key edit page, and the MSQ
form forgetting what the user submitted on a validation error.

The confirm-dialog tests decode attributes the way a browser does before
handing them to the JavaScript parser: entity decoding happens first, so an
escaped template value inside an onsubmit attribute still reaches the script
engine as live characters. What matters is what the JS parser sees, not what
the raw HTML text shows.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

EVIL = "x');alert(1)//"


class _Page(HTMLParser):
    """Every start tag's attributes, entity-decoded like a browser reads them,
    plus the page's visible text nodes."""

    def __init__(self):
        super().__init__()
        self.attrs: list[dict[str, str | None]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.attrs.append(dict(attrs))

    def handle_data(self, data):
        self.text.append(data)


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    page.close()
    return page


def _onsubmits(html: str) -> list[str]:
    return [
        value
        for attrs in _parse(html).attrs
        if (value := attrs.get("onsubmit")) is not None
    ]


def _confirm_names(html: str) -> list[str]:
    return [
        value
        for attrs in _parse(html).attrs
        if (value := attrs.get("data-confirm-name")) is not None
    ]


def test_settings_revoke_confirm_never_embeds_account_names_in_script(configured_client):
    """SMA account names come from external clients and render unapproved on
    this page, so a name must never be baked into the onsubmit script."""
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    for name in (EVIL, "O'Brien"):
        db.create_system_account(
            name=name,
            public_key="public-key-material",
            key_fingerprint="ab:cd",
            redirect_uri="https://client.example.com/callback",
            scopes='["midware.read:routes"]',
            payload_hash="payload-hash",
            pending_token_hash="pending-hash",
        )

    html = client.get("/admin/settings").get_data(as_text=True)
    scripts = _onsubmits(html)
    names = _confirm_names(html)

    # neither the payload nor a plain apostrophe reaches the JS parser
    assert not any(EVIL in script for script in scripts)
    assert not any("O'Brien" in script for script in scripts)
    # the names ride in a data attribute instead, escaped as HTML
    assert EVIL in names
    assert "O'Brien" in names
    # the handler reads the dataset and keeps its visible message
    assert any(
        "this.dataset.confirmName" in script
        and "Revoke " in script
        and "Keys it created will be deleted." in script
        for script in scripts
    )


def test_routes_delete_confirm_never_embeds_route_names_in_script(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    db.create_route(name=EVIL, target_host="https://evil.example.com", upstream_key="sk-1", is_active=False)
    db.create_route(name="Pat's box", target_host="https://pat.example.com", upstream_key="sk-2", is_active=False)

    html = client.get("/admin/routes").get_data(as_text=True)
    scripts = _onsubmits(html)
    names = _confirm_names(html)

    assert not any(EVIL in script for script in scripts)
    assert not any("Pat's box" in script for script in scripts)
    assert EVIL in names
    assert "Pat's box" in names
    assert any(
        "this.dataset.confirmName" in script and "Delete route" in script
        for script in scripts
    )


def test_premodels_delete_confirm_never_embeds_premodel_names_in_script(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    db.create_premodel(name=EVIL, slug="evil", model="m")
    db.create_premodel(name="Pat's preset", slug="pats-preset", model="m")

    html = client.get("/admin/premodels").get_data(as_text=True)
    scripts = _onsubmits(html)
    names = _confirm_names(html)

    assert not any(EVIL in script for script in scripts)
    assert not any("Pat's preset" in script for script in scripts)
    assert EVIL in names
    assert "Pat's preset" in names
    assert any(
        "this.dataset.confirmName" in script and "Delete premodel" in script
        for script in scripts
    )


def test_keys_page_credential_example_is_wellformed(configured_client):
    """The intro and the disclosure both show how to send a key: closed
    <code> markup and the README's mw-xxxxxxxx placeholder, not mangled
    entity soup or asterisks."""
    client, _ = configured_client
    html = client.get("/admin/keys").get_data(as_text=True)

    assert "&lt;ke" not in html
    assert "***" not in html
    assert html.count("<code>Authorization: Bearer mw-xxxxxxxx</code>") == 2
    assert html.count("<code>x-api-key: mw-xxxxxxxx</code>") == 2


def test_key_edit_page_masks_the_token(configured_client):
    """The edit page shows the credential the way the keys list does: masked
    bullets plus the last four characters, never the raw token as page text."""
    client, token = configured_client
    db = client.application.extensions["midware_db"]
    key_id = db.list_api_keys()[0]["id"]

    html = client.get(f"/admin/keys/{key_id}/edit").get_data(as_text=True)
    page = _parse(html)
    visible = "".join(page.text)
    masked = "••••••••" + token[-4:]

    # the raw token is never printed as page text
    assert token not in visible
    # the masked reveal and one-click copy from the keys list are here
    assert masked in visible
    reveal = [a for a in page.attrs if a.get("data-reveal-token")]
    assert len(reveal) == 1
    assert reveal[0]["data-reveal-token"] == token
    assert reveal[0]["data-masked"] == masked
    copies = [a for a in page.attrs if a.get("data-copy-token")]
    assert len(copies) == 1
    assert copies[0]["data-copy-token"] == token
    # and the wiring that makes reveal and copy answer is on the page
    assert "clipboard.js" in html
    assert "token.js" in html


def test_msq_form_keeps_submitted_values_on_validation_error(configured_client):
    """A validation error re-renders what the user submitted, not the
    defaults: values lost here get saved as a recorder configured by
    accident once the model is fixed."""
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]

    response = client.post(
        "/admin/msq/new",
        data={
            "name": "Watcher",
            "route_id": str(route_id),
            "model": "",  # the one invalid field
            "system_prompt": "Be terse.",
            "user_prompt": "Say something.",
            "interval_minutes": "15",
            "max_ttft_seconds": "12",
            "max_total_seconds": "90",
            "hc_needle": "1",
            "needle_context_chars": "9000",
            "hc_stream_integrity": "1",
        },
    )
    assert response.status_code == 200
    assert db.count_msq_recorders() == 0

    html = response.get_data(as_text=True)
    assert 'value="Watcher"' in html
    assert re.search(r'name="interval_minutes"[^>]*value="15\.0"', html)
    assert re.search(r'name="max_ttft_seconds"[^>]*value="12"', html)
    assert re.search(r'name="max_total_seconds"[^>]*value="90"', html)
    assert re.search(r'name="needle_context_chars"[^>]*value="9000"', html)
    assert re.search(r'name="hc_needle" value="1"[^>]*\bchecked\b', html)
    assert re.search(r'name="hc_stream_integrity" value="1"[^>]*\bchecked\b', html)


def test_msq_delete_confirm_never_embeds_recorder_names_in_script(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    db.create_msq_recorder(name=EVIL, route_id=db.active_route()["id"], model="m")

    html = client.get("/admin/msq/").get_data(as_text=True)
    scripts = _onsubmits(html)
    names = _confirm_names(html)

    assert not any(EVIL in script for script in scripts)
    assert EVIL in names
    assert any(
        "this.dataset.confirmName" in script and "Delete recorder" in script
        for script in scripts
    )
