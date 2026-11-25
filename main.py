from __future__ import annotations

import os
import re
import time
import smtplib
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import (
    Locator,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
import unicodedata

LISBON_TZ = ZoneInfo("Europe/Lisbon")
CP_URL = "https://cp.pt/pt/"
GITHUB_API = "https://api.github.com"
_GITHUB_VARIABLE_CACHE: dict[str, str] = {}


def get_config_value(name: str) -> str:
    """Lê primeiro o ambiente do workflow e, em falta, a GitHub Actions Variable."""
    value = os.getenv(name, "").strip()
    if value:
        return value

    if name in _GITHUB_VARIABLE_CACHE:
        return _GITHUB_VARIABLE_CACHE[name]

    token = os.getenv("GH_PAT", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        return ""

    url = f"{GITHUB_API}/repos/{repo}/actions/variables/{name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            value = str(response.json().get("value", "")).strip()
            _GITHUB_VARIABLE_CACHE[name] = value
            return value
        if response.status_code not in (403, 404):
            print(f"[AVISO CONFIG] Não foi possível ler a GitHub Variable {name}: HTTP {response.status_code}.")
    except requests.RequestException as exc:
        print(f"[AVISO CONFIG] Falha ao ler a GitHub Variable {name}: {exc}")
    return ""


# =============================================================================
# 1. ESTADO NO GITHUB E NOTIFICAÇÕES
# =============================================================================

def update_github_variable(var_name: str, value: str) -> bool:
    token = os.getenv("GH_PAT")
    repo = os.getenv("GITHUB_REPOSITORY")

    if not token or not repo:
        print(f"[AVISO] GH_PAT ou GITHUB_REPOSITORY em falta. Não foi possível atualizar {var_name}.")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"name": var_name, "value": value}
    url = f"{GITHUB_API}/repos/{repo}/actions/variables/{var_name}"

    try:
        response = requests.patch(url, headers=headers, json=payload, timeout=20)

        if response.status_code == 404:
            create_url = f"{GITHUB_API}/repos/{repo}/actions/variables"
            response = requests.post(create_url, headers=headers, json=payload, timeout=20)

        if response.status_code in (200, 201, 204):
            print(f"[ESTADO] Variável {var_name} atualizada para {value}.")
            return True

        print(
            f"[ERRO GITHUB] Não foi possível atualizar {var_name}: "
            f"HTTP {response.status_code} - {response.text[:1000]}"
        )
        return False
    except requests.RequestException as exc:
        print(f"[ERRO GITHUB] Falha de ligação ao atualizar {var_name}: {exc}")
        return False


def send_email(
    subject: str,
    body: str,
    pdf_bytes: bytes | None = None,
    pdf_filename: str = "bilhete_cp.pdf",
) -> bool:
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    notify_email = os.getenv("NOTIFY_EMAIL")

    if not all((smtp_user, smtp_pass, notify_email)):
        print("[AVISO] Credenciais SMTP incompletas. E-mail não enviado.")
        return False

    message = MIMEMultipart()
    message["From"] = smtp_user
    message["To"] = notify_email
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))

    if pdf_bytes:
        attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
        attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=pdf_filename,
        )
        message.attach(attachment)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=25) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(message)
        print(f"[EMAIL] Enviado para {notify_email}. Assunto: {subject}")
        return True
    except Exception as exc:
        print(f"[ERRO EMAIL] Não foi possível enviar o e-mail: {exc}")
        return False


# =============================================================================
# 2. FUNÇÕES AUXILIARES PLAYWRIGHT
# =============================================================================

def locator_visible(locator: Locator, timeout_ms: int = 3000) -> bool:
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False
    except Exception:
        return False


def first_existing(page, selectors: list[str]) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return None


def handle_cookies(page) -> None:
    # Preferir rejeitar cookies opcionais. Se a opção não existir, aceitar para
    # retirar o painel que impede a interação com o formulário.
    selectors = [
        "#onetrust-reject-all-handler",
        "button:has-text('Rejeitar todos')",
        "#onetrust-accept-btn-handler",
        "button:has-text('Aceitar todos')",
    ]

    for selector in selectors:
        button = page.locator(selector).first
        if locator_visible(button, 1200):
            try:
                button.click(timeout=3000)
                page.wait_for_timeout(500)
                print(f"[COOKIES] Ação efetuada: {selector}")
                return
            except Exception:
                continue


def dismiss_blocking_modal(page) -> bool:
    """Fecha apenas diálogos informativos com um controlo de fecho explícito.

    Não clica em botões genéricos como Comprar, Continuar ou Confirmar, porque
    esses controlos podem avançar ou concluir uma operação.
    """
    modal_selectors = [
        "[role='dialog']:visible",
        "[aria-modal='true']:visible",
        ".modal.show:visible",
        ".modal[open]:visible",
    ]
    close_selectors = [
        "button[aria-label*='fechar' i]",
        "button[title*='fechar' i]",
        "button[aria-label*='close' i]",
        "button[title*='close' i]",
        "button:has-text('Fechar')",
        "button:has-text('FECHAR')",
        "button:has-text('Entendi')",
        "button:has-text('OK')",
        "button:has-text('Ok')",
        "button:has-text('×')",
        ".btn-close",
        ".close",
    ]

    for modal_selector in modal_selectors:
        modals = page.locator(modal_selector)
        try:
            modal_count = min(modals.count(), 5)
        except Exception:
            continue

        for index in range(modal_count):
            modal = modals.nth(index)
            if not locator_visible(modal, 500):
                continue

            close_button = None
            for close_selector in close_selectors:
                candidate = modal.locator(close_selector).first
                try:
                    if candidate.count() > 0 and locator_visible(candidate, 500):
                        close_button = candidate
                        break
                except Exception:
                    continue

            if close_button is None:
                modal_text = " ".join(modal.inner_text().split())[:250]
                print(
                    "[MODAL] Existe um diálogo visível sem botão de fecho seguro. "
                    f"Não vou avançar. Conteúdo: {modal_text}"
                )
                return False

            try:
                close_button.click(timeout=3000)
                modal.wait_for(state="hidden", timeout=5000)
                page.wait_for_timeout(300)
            except Exception as exc:
                print(f"[MODAL] Não foi possível fechar o diálogo: {exc}")
                return False

    # Verificação adicional: não deixar um diálogo modal aberto por cima da página.
    for selector in modal_selectors:
        try:
            candidates = page.locator(selector)
            for index in range(min(candidates.count(), 5)):
                if locator_visible(candidates.nth(index), 300):
                    print("[MODAL] Continua a existir um diálogo a bloquear a página.")
                    return False
        except Exception:
            pass

    return True


def _normalize_text(value: str) -> str:
    """Remove acentos e normaliza espaços/minúsculas para comparação flexível."""
    nfkd = unicodedata.normalize("NFKD", value or "")
    without_accents = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_accents).strip().lower()


def _normalize_text(value: str) -> str:
    """Remove acentos e normaliza espaços/minúsculas para comparação flexível."""
    nfkd = unicodedata.normalize("NFKD", value or "")
    without_accents = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_accents).strip().lower()


def fill_station_input(page, input_locator: Locator, station_name: str) -> bool:
    """Preenche a estação e seleciona uma sugestão do autocomplete ignorando acentos."""
    if input_locator.count() == 0:
        print(f"[FORM] Campo da estação não encontrado: {station_name}")
        return False

    suggestion_selectors = [
        "[role='option']",
        "[role='listbox'] [role='option']",
        ".ui-autocomplete li",
        "[class*='autocomplete'] li",
        "[class*='autocomplete'] [role='option']",
        "[class*='suggestion']",
        "[class*='dropdown'] li",
        "[class*='dropdown'] [role='option']",
        "ul li",
    ]
    target_norm = _normalize_text(station_name)
    keywords = [w for w in target_norm.replace("-", " ").split() if len(w) >= 3]
    primary_token = keywords[-1] if keywords else target_norm

    for attempt in range(1, 5):
        try:
            page.wait_for_timeout(400 if attempt == 1 else 800)
            input_locator.scroll_into_view_if_needed(timeout=4000)
            input_locator.click(timeout=5000)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(250)
            query_text = station_name if attempt <= 2 else (keywords[-1] if keywords else station_name)
            input_locator.press_sequentially(query_text, delay=100)
            page.wait_for_timeout(2200)

            all_suggestions = page.locator(", ".join(suggestion_selectors))
            total = min(all_suggestions.count(), 60)

            best_suggestion = None
            seen_texts: list[str] = []
            for idx in range(total):
                cand = all_suggestions.nth(idx)
                try:
                    if not locator_visible(cand, 500):
                        continue
                    cand_text = " ".join((cand.inner_text() or "").split())
                    if not cand_text or len(cand_text) > 120:
                        continue
                    seen_texts.append(cand_text)
                    cand_norm = _normalize_text(cand_text)
                    if target_norm in cand_norm or (primary_token and primary_token in cand_norm):
                        best_suggestion = cand
                        break
                except Exception:
                    continue

            if best_suggestion is not None:
                chosen_label = " ".join((best_suggestion.inner_text() or "").split())
                best_suggestion.click(timeout=5000)
                page.wait_for_timeout(400)
                print(f"[FORM] Estação selecionada para '{station_name}': {chosen_label}")
                return True

            print(
                f"[AVISO FORM] Tentativa {attempt}: sugestão não encontrada para '{station_name}'. "
                f"Sugestões visíveis: {seen_texts[:6]}"
            )
        except Exception as exc:
            print(f"[AVISO FORM] Tentativa {attempt} para '{station_name}': {exc}")

    return False


def find_specific_train_card(page, trip_time: str, timeout_seconds: int = 25) -> tuple[Locator, str] | None:
    """Encontra o controlo 'Selecionar ida' pertencente ao IC da hora pedida."""
    time_pattern = re.compile(rf"(?<!\d){re.escape(trip_time)}(?!\d)")
    departure_time_pattern = re.compile(rf"partida\s+às\s+{re.escape(trip_time)}(?!\d)", re.IGNORECASE)
    select_pattern = re.compile(r"Selecionar\s+ida", re.IGNORECASE)
    contexts: list[tuple[str, Any]] = [("página principal", page)]

    try:
        for index, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            contexts.append((f"iframe {index} ({frame.url})", frame))
    except Exception:
        pass

    ancestor_script = r"""(element, requestedTime) => {
        const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
        const escaped = requestedTime.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const timeRegex = new RegExp('(^|\\D)' + escaped + '(?!\\d)');
        const serviceRegex = /intercidades|\bIC\b/i;
        let current = element;
        let best = null;
        for (let level = 0; current && level < 14; level += 1, current = current.parentElement) {
            const text = normalize(current.innerText || current.textContent || '');
            const selectMatches = text.match(/selecionar\s+ida/gi) || [];
            const departureMatches = text.match(/partida\s+às/gi) || [];
            if (selectMatches.length > 1 || departureMatches.length > 1) {
                continue;
            }
            if (text.length >= 20 && text.length <= 5000 && timeRegex.test(text) && serviceRegex.test(text)) {
                if (best === null || text.length < best.text.length) {
                    best = { text, level };
                }
            }
        }
        return best;
    }"""

    deadline = time.monotonic() + max(5, timeout_seconds)
    announced_wait = False
    last_diagnostics: list[str] = []

    while time.monotonic() < deadline:
        last_diagnostics = []

        for context_name, context in contexts:
            try:
                controls = context.locator(
                    "button:visible, a:visible, [role='button']:visible"
                ).filter(has_text=select_pattern)
                control_count = min(controls.count(), 80)
            except Exception:
                control_count = 0
                controls = None

            if control_count:
                last_diagnostics.append(f"{context_name}: {control_count} controlo(s) 'Selecionar ida'")
                for index in range(control_count):
                    control = controls.nth(index)
                    try:
                        if not control.is_visible(timeout=350):
                            continue
                        match = control.evaluate(ancestor_script, trip_time)
                    except Exception:
                        continue

                    if not match:
                        continue
                    card_text = " ".join(str(match.get("text", "")).split())
                    if not time_pattern.search(card_text):
                        continue
                    if not re.search(r"Intercidades|\bIC\b", card_text, re.IGNORECASE):
                        continue
                    if "partida às" in card_text.lower() and not departure_time_pattern.search(card_text):
                        continue

                    print(
                        f"[COMBOIO] Cartão Intercidades das {trip_time} encontrado em "
                        f"{context_name}; controlo de seleção validado."
                    )
                    return control, card_text

            try:
                time_nodes = context.get_by_text(time_pattern)
                node_count = min(time_nodes.count(), 30)
            except Exception:
                node_count = 0
                time_nodes = None

            for node_index in range(node_count):
                node = time_nodes.nth(node_index)
                try:
                    if not node.is_visible(timeout=300):
                        continue
                    match = node.evaluate(ancestor_script, trip_time)
                    if not match:
                        continue

                    card_text = " ".join(str(match.get("text", "")).split())
                    if not time_pattern.search(card_text):
                        continue
                    if not re.search(r"Intercidades|\bIC\b", card_text, re.IGNORECASE):
                        continue

                    buttons = context.locator(
                        "button:visible, a:visible, [role='button']:visible"
                    ).filter(has_text=select_pattern)
                    for button_index in range(min(buttons.count(), 80)):
                        button = buttons.nth(button_index)
                        try:
                            button_match = button.evaluate(ancestor_script, trip_time)
                            if button_match and len(button_match.get("text", "")) <= max(5000, len(match.get("text", "")) + 300):
                                button_card = " ".join(button_match.get("text", "").split())
                                if time_pattern.search(button_card) and re.search(
                                    r"Intercidades|\bIC\b", button_card, re.IGNORECASE
                                ):
                                    if "partida às" in button_card.lower() and not departure_time_pattern.search(button_card):
                                        continue
                                    return button, button_card
                        except Exception:
                            continue
                except Exception:
                    continue

            try:
                body_text = " ".join(context.locator("body").inner_text(timeout=1200).split())
                if body_text:
                    time_pos = body_text.find(trip_time)
                    if time_pos >= 0:
                        snippet = body_text[max(0, time_pos - 120):time_pos + 260]
                        last_diagnostics.append(f"{context_name}: texto junto da hora: {snippet}")
                    elif re.search(r"Intercidades|\bIC\b", body_text, re.IGNORECASE):
                        match = re.search(r"Intercidades|\bIC\b", body_text, re.IGNORECASE)
                        snippet = body_text[max(0, match.start() - 80):match.start() + 300]
                        last_diagnostics.append(f"{context_name}: resultados IC visíveis: {snippet}")
            except Exception:
                pass

        if not announced_wait:
            print(
                f"[COMBOIO] Resultados ainda sem o Intercidades das {trip_time}; "
                f"a aguardar até {timeout_seconds}s por carregamento dinâmico."
            )
            announced_wait = True
        page.wait_for_timeout(1000)

    for diagnostic in last_diagnostics[:6]:
        print(f"[DEBUG COMBOIO] {diagnostic}")

    print(
        f"[COMBOIO] Não foi possível validar o botão 'Selecionar ida' associado "
        f"ao Intercidades das {trip_time} após {timeout_seconds}s."
    )
    return None


def select_coach_and_seat(page) -> str:
    """Tenta escolher as carruagens/lugares preferidos; mantém automático se não der."""
    default_message = "Lugar automático atribuído pelo sistema"

    try:
        coach_candidates = [
            "[data-coach]",
            ".coach-selector button",
            ".carruagem-item",
            "button[aria-label*='carruagem' i]",
        ]
        coach_locator = page.locator(", ".join(coach_candidates))

        selected_coach: str | None = None
        for coach_number in ("24", "23"):
            candidate = page.locator(
                f"[data-coach='{coach_number}'], "
                f".carruagem-item:has-text('{coach_number}'), "
                f"button[aria-label*='carruagem {coach_number}' i]"
            ).first
            try:
                if candidate.count() and locator_visible(candidate, 800) and candidate.is_enabled():
                    candidate.click(timeout=3000)
                    selected_coach = coach_number
                    page.wait_for_timeout(700)
                    break
            except Exception:
                continue

        if selected_coach is None and coach_locator.count() > 0:
            # Não assumir que o último elemento é uma carruagem válida se for
            # apenas uma opção de um <select> ou um elemento oculto.
            for index in range(coach_locator.count() - 1, -1, -1):
                candidate = coach_locator.nth(index)
                try:
                    if locator_visible(candidate, 300) and candidate.is_enabled():
                        candidate.click(timeout=3000)
                        selected_coach = "selecionada automaticamente"
                        page.wait_for_timeout(700)
                        break
                except Exception:
                    continue

        for seat_number in ("68", "63"):
            seat = page.locator(
                f"[data-seat='{seat_number}']:not(.occupied):not(.unavailable):not([disabled]), "
                f".seat.available:has-text('{seat_number}'), "
                f"button.lugar-livre:has-text('{seat_number}')"
            ).first
            try:
                if seat.count() and locator_visible(seat, 700) and seat.is_enabled():
                    seat.click(timeout=3000)
                    result = f"Carruagem {selected_coach or 'atribuída pelo sistema'}, Lugar {seat_number}"
                    print(f"[LUGAR] Selecionado: {result}")
                    return result
            except Exception:
                continue

        if selected_coach:
            return f"Carruagem {selected_coach}; lugar automático (68/63 indisponíveis)"

    except Exception as exc:
        print(f"[AVISO LUGAR] Não foi possível personalizar o lugar: {exc}")

    return default_message


def parse_euro_amount(raw_amount: str) -> float | None:
    value = raw_amount.strip().replace("\u00a0", "").replace(" ", "")
    if "," in value and "." in value:
        # Em Portugal, o ponto costuma separar milhares e a vírgula os cêntimos.
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def verify_zero_euro_total(page) -> bool:
    """Só aceita 0,00 € quando o montante aparece associado a um rótulo de total/resumo."""
    try:
        body_text = page.locator("body").inner_text(timeout=7000)
    except Exception as exc:
        print(f"[SEGURANÇA] Não foi possível ler o resumo da compra: {exc}")
        return False

    normalized = re.sub(r"\s+", " ", body_text)
    amount_pattern = r"(?:€\s*)?(\d{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})|\d+[,.]\d{2}|\d+)\s*€?"
    label_pattern = re.compile(
        rf"(?:Total(?:\s+(?:a\s+pagar|da\s+viagem|da\s+compra|geral|líquido|liquido))?"
        rf"|Valor\s+(?:total|a\s+pagar)|A\s+pagar|Subtotal|Preço\s+total|Preco\s+total)"
        rf"\s*[:\-]?\s*.{{0,65}}?{amount_pattern}",
        re.IGNORECASE,
    )

    matches = list(label_pattern.finditer(normalized))
    if not matches:
        # Fallback: procura padrões explícitos de zero euros junto a resumo/tarifa/passe
        zero_context_pattern = re.compile(
            r"(?:total|pagar|tarifa|desconto|passe\s+ferrovi[aá]rio\s+verde).{0,60}?(?:0[,.]00\s*€|€\s*0[,.]00)",
            re.IGNORECASE,
        )
        if zero_context_pattern.search(normalized):
            print("[SEGURANÇA] Total de 0,00 € detetado no contexto do resumo/tarifa.")
            return True

        print("[SEGURANÇA] Não foi encontrado um valor associado ao rótulo Total/A pagar. Compra abortada.")
        print(f"[DEBUG SEGURANÇA ECRÃ] {normalized[-800:]}")
        return False

    amounts: list[float] = []
    for match in matches:
        amount = parse_euro_amount(match.group(1))
        if amount is not None:
            amounts.append(amount)

    if not amounts:
        print("[SEGURANÇA] O total foi encontrado, mas não foi possível interpretar o montante.")
        return False

    final_amount = amounts[-1]
    print(f"[SEGURANÇA] Total associado ao resumo: {final_amount:.2f} €")
    return abs(final_amount) < 0.005


def find_visible_button(page, labels: list[str]) -> Locator | None:
    for label in labels:
        regex = re.compile(rf"^\s*{re.escape(label)}\s*$", re.IGNORECASE)
        for role in ("button", "link"):
            candidate = page.get_by_role(role, name=regex).last
            try:
                if candidate.count() > 0 and locator_visible(candidate, 700):
                    return candidate
            except Exception:
                pass
    return None


def fill_required_field(page, selectors: list[str], value: str, field_label: str) -> bool:
    """Preenche o primeiro campo visível e dispara eventos de validação."""
    locator = first_existing(page, selectors)
    if locator is None:
        print(f"[AVISO FORM] Campo '{field_label}' não encontrado pelos seletores.")
        return False

    try:
        locator.scroll_into_view_if_needed(timeout=4000)
        locator.click(timeout=5000)
        locator.fill(value, timeout=5000)
        locator.dispatch_event("input")
        locator.dispatch_event("change")
        locator.press("Tab")
        page.wait_for_timeout(250)
        actual = (locator.input_value() or "").strip()
        if not actual:
            print(f"[AVISO FORM] Campo '{field_label}' continuou vazio após preenchimento.")
            return False
        print(f"[PASSAGEIRO] {field_label} preenchido e validado.")
        return True
    except Exception as exc:
        print(f"[AVISO FORM] Falha ao preencher '{field_label}': {exc}")
        return False


def select_green_pass(page) -> bool:
    pass_pattern = re.compile(r"ferroviario\s*verde|passe\s*verde", re.IGNORECASE)

    try:
        body_text = " ".join((page.locator("body").inner_text(timeout=3000) or "").split())
    except Exception:
        body_text = ""

    if not pass_pattern.search(_normalize_text(body_text)):
        print("[DESCONTO] Opção de Passe Ferroviário Verde não presente neste ecrã; a prosseguir.")
        return True

    try:
        selects = page.locator("select")
        for idx in range(selects.count()):
            sel = selects.nth(idx)
            options = sel.locator("option").all_text_contents()
            for opt in options:
                clean_opt = " ".join(opt.split())
                if pass_pattern.search(_normalize_text(clean_opt)):
                    sel.select_option(label=opt)
                    print(f"[DESCONTO] Selecionado no <select>: {clean_opt}")
                    return True
    except Exception as exc:
        print(f"[DESCONTO] Aviso ao inspecionar <select>: {exc}")

    trigger_selectors = [
        "[role='combobox']",
        "[aria-haspopup='listbox']",
        "button[aria-label*='desconto' i]",
        "button[aria-label*='tarifa' i]",
        "button:has-text('Desconto')",
        "button:has-text('Sem desconto')",
        "label:has-text('Desconto')",
        "label:has-text('Tarifa')",
    ]

    for trig_sel in trigger_selectors:
        triggers = page.locator(trig_sel)
        for t_idx in range(min(triggers.count(), 6)):
            trig = triggers.nth(t_idx)
            try:
                if not locator_visible(trig, 600):
                    continue
                trig.scroll_into_view_if_needed(timeout=3000)
                trig.click(timeout=4000)
                page.wait_for_timeout(700)

                option_candidates = page.locator(
                    "[role='option'], [role='menuitem'], [role='radio'], li, button, div, span"
                )
                for c_idx in range(min(option_candidates.count(), 120)):
                    cand = option_candidates.nth(c_idx)
                    try:
                        if not locator_visible(cand, 300):
                            continue
                        txt = " ".join((cand.inner_text() or "").split())
                        if txt and len(txt) < 140 and pass_pattern.search(_normalize_text(txt)):
                            cand.click(timeout=4000)
                            print(f"[DESCONTO] Selecionado na lista: {txt}")
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

    print("[DESCONTO] Não foi possível selecionar Passe Ferroviário Verde.")
    return False

def find_additional_info_input(page) -> Locator | None:
    """Localiza o <input> associado a 'Informação adicional' por vários caminhos."""
    label_pattern = re.compile(r"informa[çc][aã]o\s+adicional", re.IGNORECASE)

    # 1. Atributos diretos ou via <label for="..."> / aria-labelledby
    direct_selectors = [
        "input[aria-label*='adicional' i]",
        "input[placeholder*='adicional' i]",
        "input[name*='adicional' i]",
        "input[id*='adicional' i]",
    ]
    direct = first_existing(page, direct_selectors)
    if direct is not None and locator_visible(direct, 1000):
        return direct

    label_el = page.locator("label, span, div, p").filter(has_text=label_pattern)
    for idx in range(min(label_el.count(), 10)):
        lbl = label_el.nth(idx)
        try:
            if not locator_visible(lbl, 500):
                continue
            for_attr = (lbl.get_attribute("for") or "").strip()
            if for_attr:
                by_id = page.locator(f"[id='{for_attr}']").first
                if by_id.count() > 0 and locator_visible(by_id, 600):
                    return by_id

            lbl_id = (lbl.get_attribute("id") or "").strip()
            if lbl_id:
                by_labelledby = page.locator(f"[aria-labelledby~='{lbl_id}']").first
                if by_labelledby.count() > 0 and locator_visible(by_labelledby, 600):
                    return by_labelledby

            # 2. Subir até 5 ancestrais e procurar input visível no contentor
            container = lbl
            for _ in range(5):
                container = container.locator("..")
                if container.count() == 0:
                    break
                inp = container.locator(
                    "input:visible:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
                ).first
                if inp.count() > 0 and locator_visible(inp, 500):
                    return inp
        except Exception:
            continue

    # 3. Proximidade geométrica ao texto 'Informação adicional'
    text_node = page.get_by_text(label_pattern).first
    try:
        if text_node.count() > 0 and locator_visible(text_node, 800):
            t_box = text_node.bounding_box()
            if t_box:
                t_cx = t_box["x"] + t_box["width"] / 2
                t_cy = t_box["y"] + t_box["height"] / 2
                inputs = page.locator(
                    "input:visible:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
                )
                best_inp = None
                best_dist = float("inf")
                for i in range(inputs.count()):
                    cand = inputs.nth(i)
                    box = cand.bounding_box()
                    if not box:
                        continue
                    c_cx = box["x"] + box["width"] / 2
                    c_cy = box["y"] + box["height"] / 2
                    # Aceita campos à direita na mesma linha ou logo abaixo
                    same_row_right = abs(c_cy - t_cy) < 60 and c_cx >= t_box["x"]
                    below_near = 0 <= (c_cy - t_cy) < 140 and abs(c_cx - t_cx) < 350
                    if same_row_right or below_near:
                        dist = ((c_cx - t_cx) ** 2 + (c_cy - t_cy) ** 2) ** 0.5
                        if dist < best_dist:
                            best_dist = dist
                            best_inp = cand
                if best_inp is not None:
                    return best_inp
    except Exception:
        pass

    return None


def find_name_input(page) -> Locator | None:
    """Localiza o <input> associado a 'Nome e apelido' por atributos, rótulo ou proximidade."""
    name_selectors = [
        "input[name*='nome' i]",
        "input[id*='nome' i]",
        "input[aria-label*='nome' i]",
        "input[placeholder*='Nome' i]",
    ]
    direct = first_existing(page, name_selectors)
    if direct is not None and locator_visible(direct, 1000):
        return direct

    label_pattern = re.compile(r"nome\s+e\s+apelido|^\s*nome\s*\*?\s*$", re.IGNORECASE)
    label_els = page.locator("label, span, div, p").filter(has_text=label_pattern)
    for idx in range(min(label_els.count(), 10)):
        lbl = label_els.nth(idx)
        try:
            if not locator_visible(lbl, 500):
                continue
            for_attr = (lbl.get_attribute("for") or "").strip()
            if for_attr:
                by_id = page.locator(f"[id='{for_attr}']").first
                if by_id.count() > 0 and locator_visible(by_id, 600):
                    return by_id

            lbl_id = (lbl.get_attribute("id") or "").strip()
            if lbl_id:
                by_labelledby = page.locator(f"[aria-labelledby~='{lbl_id}']").first
                if by_labelledby.count() > 0 and locator_visible(by_labelledby, 600):
                    return by_labelledby

            container = lbl
            for _ in range(5):
                container = container.locator("..")
                if container.count() == 0:
                    break
                inp = container.locator(
                    "input:visible:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
                ).first
                if inp.count() > 0 and locator_visible(inp, 500):
                    return inp
        except Exception:
            continue

    text_node = page.get_by_text(re.compile(r"nome\s+e\s+apelido", re.IGNORECASE)).first
    try:
        if text_node.count() > 0 and locator_visible(text_node, 800):
            t_box = text_node.bounding_box()
            if t_box:
                t_cx = t_box["x"] + t_box["width"] / 2
                t_cy = t_box["y"] + t_box["height"] / 2
                inputs = page.locator(
                    "input:visible:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
                )
                best_inp = None
                best_dist = float("inf")
                for i in range(inputs.count()):
                    cand = inputs.nth(i)
                    box = cand.bounding_box()
                    if not box:
                        continue
                    c_cx = box["x"] + box["width"] / 2
                    c_cy = box["y"] + box["height"] / 2
                    same_row_right = abs(c_cy - t_cy) < 60 and c_cx >= t_box["x"]
                    below_near = 0 <= (c_cy - t_cy) < 140 and abs(c_cx - t_cx) < 350
                    if same_row_right or below_near:
                        dist = ((c_cx - t_cx) ** 2 + (c_cy - t_cy) ** 2) ** 0.5
                        if dist < best_dist:
                            best_dist = dist
                            best_inp = cand
                if best_inp is not None:
                    return best_inp
    except Exception:
        pass

    return None


def set_checkbox_for_statement(page, pattern: re.Pattern[str], description: str) -> bool:
    """Marca e verifica uma declaração específica, sem tocar em opções de marketing."""
    checkbox = page.get_by_role("checkbox", name=pattern).last
    try:
        if checkbox.count() > 0:
            if not checkbox.is_checked():
                checkbox.check(force=True, timeout=4000)
            if checkbox.is_checked():
                print(f"[DECLARAÇÃO] Confirmada: {description}")
                return True
    except Exception:
        pass

    labels = page.locator("label").filter(has_text=pattern)
    try:
        for index in range(labels.count()):
            label = labels.nth(index)
            nested_checkbox = label.locator("input[type='checkbox']").first
            try:
                if nested_checkbox.count() > 0:
                    if not nested_checkbox.is_checked():
                        nested_checkbox.check(force=True, timeout=4000)
                    if nested_checkbox.is_checked():
                        print(f"[DECLARAÇÃO] Confirmada: {description}")
                        return True
            except Exception:
                pass

            try:
                label.click(force=True, timeout=4000)
                if nested_checkbox.count() > 0 and nested_checkbox.is_checked():
                    print(f"[DECLARAÇÃO] Confirmada: {description}")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    print(f"[DECLARAÇÃO] Não foi possível confirmar: {description}")
    return False


def continue_as_guest_if_prompted(page) -> tuple[bool, str]:
    """Aguarda o fim do ecrã de carregamento da CP e seleciona 'Continuar como convidado'."""
    guest_pattern = re.compile(
        r"Continuar\s+como\s+convidado|Prosseguir\s+como\s+convidado|Comprar\s+sem\s+registo|Sem\s+login",
        re.IGNORECASE,
    )
    auth_pattern = re.compile(
        r"Antes\s+de\s+continuar|escolha\s+como\s+prosseguir|Fazer\s+login|myCP|Iniciar\s+sessão",
        re.IGNORECASE,
    )
    loading_pattern = re.compile(r"Por\s+favor\s+aguarde\s+um\s+momento", re.IGNORECASE)
    name_selectors = [
        "input[name*='nome' i]",
        "input[placeholder*='Nome' i]",
        "input[id*='nome' i]",
    ]

    deadline = time.monotonic() + 22
    last_body = ""

    while time.monotonic() < deadline:
        try:
            last_body = " ".join(page.locator("body").inner_text(timeout=3000).split())
        except Exception:
            page.wait_for_timeout(500)
            continue

        # 1. Se o portal saltou a autenticação e já abriu o formulário do passageiro
        name_field = first_existing(page, name_selectors)
        if name_field is not None and locator_visible(name_field, 400):
            return True, "Formulário do passageiro já apresentado sem passo de autenticação."

        # 2. Se ainda está na transição "Por favor aguarde um momento...", continua a aguardar
        if loading_pattern.search(last_body) and not guest_pattern.search(last_body):
            page.wait_for_timeout(600)
            continue

        # 3. Procura o controlo de convidado (atravessa Shadow DOM via CSS/get_by_text)
        guest_button = None
        for selector in (
            "button:visible",
            "a:visible",
            "[role='button']:visible",
        ):
            candidate = page.locator(selector).filter(has_text=guest_pattern).last
            try:
                if candidate.count() > 0 and locator_visible(candidate, 400):
                    guest_button = candidate
                    break
            except Exception:
                continue

        if guest_button is None:
            candidate = page.get_by_text(guest_pattern, exact=False).last
            try:
                if candidate.count() > 0 and locator_visible(candidate, 400):
                    guest_button = candidate
            except Exception:
                pass

        if guest_button is not None:
            try:
                print("[CONVIDADO] A selecionar 'Continuar como convidado'.")
                guest_button.scroll_into_view_if_needed(timeout=4000)
                guest_button.click(timeout=10000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=12000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1200)
                print(f"[CONVIDADO] Continuação como convidado iniciada. URL: {page.url}")
                return True, "Continuação como convidado iniciada."
            except Exception as exc:
                return False, f"Não foi possível clicar em 'Continuar como convidado': {exc}"

        page.wait_for_timeout(600)

    # Se esgotou o tempo de espera, devolve diagnóstico claro do estado final
    if loading_pattern.search(last_body):
        return False, (
            "A página da CP permaneceu presa em 'Por favor aguarde um momento...' após 22s. "
            f"URL: {page.url}"
        )

    if auth_pattern.search(last_body):
        return False, (
            "A CP apresentou a página de autenticação, mas o controlo "
            "'Continuar como convidado' não ficou visível após 22s. "
            f"URL: {page.url}. Ecrã: {last_body[-900:]}"
        )

    return True, "Não foi detetado ecrã de autenticação após espera."

    try:
        print("[CONVIDADO] A selecionar 'Continuar como convidado'.")
        guest_button.scroll_into_view_if_needed(timeout=5000)
        guest_button.click(timeout=10000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=12000)
        except PlaywrightTimeoutError:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1200)
    except Exception as exc:
        return False, f"Não foi possível selecionar 'Continuar como convidado': {exc}"

    try:
        updated_body = " ".join(page.locator("body").inner_text(timeout=7000).split())
    except Exception as exc:
        return False, f"Não foi possível validar a continuação como convidado: {exc}"

    still_on_auth = (
        auth_pattern.search(updated_body) is not None
        and guest_pattern.search(updated_body) is not None
        and page.url.rstrip("/").endswith("/autenticacao")
    )
    if still_on_auth:
        return False, (
            "A opção 'Continuar como convidado' foi acionada, mas a CP manteve "
            f"a página de autenticação. URL: {page.url}. Ecrã: {updated_body[-900:]}"
        )

    print(f"[CONVIDADO] Continuação como convidado iniciada. URL: {page.url}")
    return True, "Continuação como convidado iniciada."


def prepare_intermediate_purchase_step(page) -> tuple[bool, str]:
    """Confirma declarações da página-resumo e avança para os dados do passageiro."""
    body_text = page.locator("body").inner_text(timeout=7000)
    normalized = " ".join(body_text.split())

    advance_pattern = re.compile(r"Avançar\s+para\s+compra", re.IGNORECASE)
    advance_button = page.get_by_role("button", name=advance_pattern).last
    if advance_button.count() == 0 or not locator_visible(advance_button, 1500):
        advance_button = page.get_by_role("link", name=advance_pattern).last

    # Esta etapa só é necessária quando o portal apresenta o ecrã de resumo.
    if advance_button.count() == 0 or not locator_visible(advance_button, 1500):
        if re.search(r"input.*nome|Nome do passageiro", normalized, re.IGNORECASE):
            return True, "Formulário do passageiro já disponível."
        return False, "A página não apresenta o botão 'Avançar para compra'."

    print("[COMPRA] Página-resumo detetada; a confirmar declarações obrigatórias.")

    conditions_pattern = re.compile(r"Condições\s+de\s+venda", re.IGNORECASE)
    privacy_pattern = re.compile(
        r"Termos.*Política\s+de\s+Privacidade|Política\s+de\s+Privacidade",
        re.IGNORECASE,
    )
    age_pattern = re.compile(r"Tenho\s+mais\s+de\s+16\s+anos|mais\s+de\s+16\s+anos", re.IGNORECASE)

    if re.search(conditions_pattern, normalized):
        if not set_checkbox_for_statement(page, conditions_pattern, "Condições de venda"):
            return False, "Não foi possível confirmar as Condições de venda."

    if re.search(privacy_pattern, normalized):
        if not set_checkbox_for_statement(page, privacy_pattern, "Termos e Política de Privacidade"):
            return False, "Não foi possível confirmar os Termos e a Política de Privacidade."

    if re.search(age_pattern, normalized):
        # A automação não deve declarar uma idade que não tenha sido configurada.
        if get_config_value("PASSENGER_OVER_16").strip().lower() != "true":
            return False, (
                "A página exige a declaração 'Tenho mais de 16 anos'. "
                "Defina PASSENGER_OVER_16=true apenas se a declaração for verdadeira."
            )
        if not set_checkbox_for_statement(page, age_pattern, "Mais de 16 anos"):
            return False, "Não foi possível confirmar a declaração de idade."

    # O resumo da CP pode estar dentro de um elemento com role="dialog".
    # Esse contentor é a própria página de resumo (inclui declarações e o botão
    # de avanço), não uma janela que deva ser fechada. Não chamar
    # dismiss_blocking_modal() aqui: a função interpretaria o resumo como modal
    # bloqueador por não existir um botão X/Fechar.

    # Voltar a localizar o botão após as caixas de seleção, pois a página pode
    # atualizar o estado do controlo dinamicamente.
    advance_button = page.get_by_role("button", name=advance_pattern).last
    if advance_button.count() == 0 or not locator_visible(advance_button, 2500):
        advance_button = page.get_by_role("link", name=advance_pattern).last
    if advance_button.count() == 0 or not locator_visible(advance_button, 2500):
        return False, "O botão 'Avançar para compra' desapareceu após confirmar as declarações."

    print("[COMPRA] A validar o botão 'Avançar para compra'.")
    try:
        advance_button.scroll_into_view_if_needed(timeout=5000)
        # trial=True verifica se o botão está realmente acionável e se algum
        # overlay o interceta, sem executar a ação duas vezes.
        advance_button.click(trial=True, timeout=6000)
    except Exception as exc:
        return False, (
            "O botão 'Avançar para compra' foi encontrado, mas o Playwright "
            f"detetou que não está acionável: {type(exc).__name__}: {exc}"
        )

    print("[COMPRA] A avançar para a etapa seguinte.")
    advance_button.click(timeout=10000)

    try:
        page.wait_for_load_state("domcontentloaded", timeout=12000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1000)

    guest_ok, guest_details = continue_as_guest_if_prompted(page)
    if not guest_ok:
        return False, guest_details

    name_selectors = [
        "input[name*='nome' i]",
        "input[placeholder*='Nome' i]",
        "input[id*='nome' i]",
    ]
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        name_field = first_existing(page, name_selectors)
        if name_field is not None and locator_visible(name_field, 500):
            print("[PASSAGEIROS] Formulário do passageiro apresentado.")
            return True, "Formulário do passageiro disponível."

        current_text = " ".join(page.locator("body").inner_text(timeout=3000).split())
        if "Não foi possível iniciar o processo de compra" in current_text:
            return False, "A CP informou que não foi possível iniciar o processo de compra."
        page.wait_for_timeout(500)

    current_text = " ".join(page.locator("body").inner_text(timeout=5000).split())
    return False, (
        "O botão 'Avançar para compra' foi acionado, mas o formulário do passageiro "
        f"não apareceu. URL: {page.url}. Ecrã: {current_text[-1000:]}"
    )


def accept_terms_if_present(page) -> bool:
    terms_pattern = re.compile(r"termos|condições gerais|condicoes gerais", re.IGNORECASE)
    labels = page.locator("label").filter(has_text=terms_pattern)

    try:
        for index in range(labels.count()):
            label = labels.nth(index)
            checkbox = label.locator("input[type='checkbox']").first
            if checkbox.count() == 0:
                checkbox = label.get_by_role("checkbox").first
            if checkbox.count() > 0:
                try:
                    if not checkbox.is_checked():
                        # Em muitos formulários o input está visualmente oculto;
                        # clicar no respetivo label é mais fiável e não toca nos
                        # checkboxes de publicidade.
                        label.click(timeout=3000)
                    if checkbox.is_checked():
                        return True
                except Exception:
                    try:
                        checkbox.check(timeout=3000, force=True)
                        if checkbox.is_checked():
                            return True
                    except Exception:
                        continue
    except Exception:
        pass

    # Fallback pelo nome acessível, sem selecionar checkboxes de marketing.
    checkbox = page.get_by_role(
        "checkbox",
        name=re.compile(r"termos|condições gerais|condicoes gerais", re.IGNORECASE),
    ).first
    try:
        if checkbox.count() > 0 and locator_visible(checkbox, 700):
            if not checkbox.is_checked():
                checkbox.check(timeout=3000)
            return True
    except Exception:
        pass

    # Algumas versões do portal não apresentam checkbox nesta etapa.
    print("[TERMOS] Não foi encontrado um checkbox de termos identificado por texto.")
    return False


def download_ticket_pdf(page) -> bytes | None:
    download_button = first_existing(
        page,
        [
            "a:has-text('PDF')",
            "button:has-text('PDF')",
            "a:has-text('Descarregar')",
            "button:has-text('Descarregar')",
            "a:has-text('Bilhete')",
        ],
    )

    if download_button is None or not locator_visible(download_button, 4000):
        print("[PDF] Não foi encontrado um botão de descarga do bilhete.")
        return None

    try:
        with page.expect_download(timeout=15000) as download_info:
            download_button.click(timeout=5000)
        download = download_info.value
        download_path = download.path()
        if not download_path:
            return None
        data = Path(download_path).read_bytes()
        if not data.startswith(b"%PDF"):
            print("[PDF] O ficheiro descarregado não parece ser um PDF válido.")
            return None
        print(f"[PDF] Bilhete descarregado ({len(data)} bytes).")
        return data
    except Exception as exc:
        print(f"[PDF] Não foi possível descarregar o bilhete: {exc}")
        return None


# =============================================================================
# 3. PROCESSO DE PESQUISA E EMISSÃO
# =============================================================================

def attempt_ticket_purchase(
    origin: str,
    destination: str,
    trip_date: str,
    trip_time: str,
    wait_until_dt: datetime | None = None,
) -> tuple[bool, str, bytes | None]:
    config_names = (
        "CP_PASS_CODE",
        "PASSENGER_NAME",
        "PASSENGER_ID_NUMBER",
        "PASSENGER_EMAIL",
    )
    config = {name: get_config_value(name) for name in config_names}
    missing = [name for name, value in config.items() if not value]
    if missing:
        return (
            False,
            "Dados do passageiro em falta no ambiente do workflow/GitHub Variables: "
            + ", ".join(missing)
            + ". Para GitHub Variables, GH_PAT tem de ter permissão Actions: Variables read.",
            None,
        )

    pass_code = config["CP_PASS_CODE"]
    passenger_name = config["PASSENGER_NAME"]
    passenger_id = config["PASSENGER_ID_NUMBER"]
    passenger_email = config["PASSENGER_EMAIL"]

    try:
        date_obj = datetime.strptime(trip_date, "%Y-%m-%d")
    except ValueError:
        return False, "TRIP_DATE tem de estar no formato AAAA-MM-DD.", None

    cp_date_str = date_obj.strftime("%d/%m/%Y")

    with sync_playwright() as playwright:
        browser = None
        context = None
        final_submit_attempted = False
        try:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                locale="pt-PT",
                timezone_id="Europe/Lisbon",
                viewport={"width": 1440, "height": 1000},
                accept_downloads=True,
            )
            page = context.new_page()
            page.set_default_timeout(8000)
            page.set_default_navigation_timeout(30000)

            print(
                f"[NAVEGAÇÃO] A abrir {CP_URL}: {origin} -> {destination} "
                f"({cp_date_str} às {trip_time})."
            )
            page.goto(CP_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1800)
            handle_cookies(page)

            # -----------------------------------------------------------------
            # A. Preencher origem, destino e data
            # -----------------------------------------------------------------
            visible_text_inputs = page.locator("input[type='text']:visible")
            origin_input = first_existing(
                page,
                [
                    "input[placeholder*='Origem' i]",
                    "input[aria-label*='Origem' i]",
                    "input[name*='origem' i]",
                    "input[id*='origem' i]",
                ],
            )
            destination_input = first_existing(
                page,
                [
                    "input[placeholder*='Destino' i]",
                    "input[aria-label*='Destino' i]",
                    "input[name*='destino' i]",
                    "input[id*='destino' i]",
                ],
            )

            if origin_input is None and visible_text_inputs.count() >= 2:
                origin_input = visible_text_inputs.nth(0)
            if destination_input is None and visible_text_inputs.count() >= 2:
                destination_input = visible_text_inputs.nth(1)

            if origin_input is None or destination_input is None:
                return False, "Não foram encontrados os campos de origem e destino.", None

            origin_ok = fill_station_input(page, origin_input, origin)
            destination_ok = fill_station_input(page, destination_input, destination)
            if not (origin_ok and destination_ok):
                return (
                    False,
                    f"Não foi possível selecionar as estações (origem={origin_ok}, destino={destination_ok}).",
                    None,
                )

            date_input = first_existing(
                page,
                [
                    "input[placeholder*='Data' i]",
                    "input[aria-label*='Data' i]",
                    "input[name*='data' i]",
                    "input[placeholder*='dd/mm' i]",
                    "input[name*='date' i]",
                ],
            )
            if date_input is None and visible_text_inputs.count() >= 3:
                date_input = visible_text_inputs.nth(2)

            if date_input is None or not locator_visible(date_input, 2500):
                return False, "Campo da data da viagem não encontrado.", None

            date_input.click()
            date_input.fill(cp_date_str)
            date_input.press("Tab")
            print(f"[FORM] Data preenchida: {cp_date_str}")

            # Esperar pela abertura oficial, se a execução começou antes.
            if wait_until_dt is not None:
                now = datetime.now(LISBON_TZ)
                seconds_to_wait = (wait_until_dt - now).total_seconds()
                if 0 < seconds_to_wait <= 900:
                    print(
                        f"[SYNC] A aguardar {seconds_to_wait:.1f}s até "
                        f"{wait_until_dt.strftime('%H:%M:%S')} Europe/Lisbon."
                    )
                    time.sleep(seconds_to_wait)

            search_button = first_existing(
                page,
                [
                    "button.search-button",
                    "button:has-text('Pesquisar')",
                    "button:has-text('Procurar')",
                ],
            )
            if search_button is None or not locator_visible(search_button, 4000):
                return False, "Botão de pesquisa não encontrado.", None
            if not search_button.is_enabled():
                return False, "Botão de pesquisa está desativado.", None

            search_button.click(timeout=8000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1500)
            print(f"[DEBUG URL PESQUISA] {page.url}")

            # -----------------------------------------------------------------
            # B. Escolher o Intercidades exato
            # -----------------------------------------------------------------
            train_result = find_specific_train_card(page, trip_time, timeout_seconds=25)
            if train_result is None:
                return False, f"Comboio Intercidades das {trip_time} não encontrado/validado nos resultados.", None

            select_outbound, card_text = train_result
            print(f"[DEBUG COMBOIO] {card_text[:1200]}")
            if not re.search(r"Intercidades|\bIC\b", card_text, re.IGNORECASE):
                return False, "O cartão localizado não foi identificado como Intercidades.", None
            if not re.search(rf"(?<!\d){re.escape(trip_time)}(?!\d)", card_text):
                return False, "A hora do cartão localizado não corresponde à hora pedida.", None
            if "partida às" in card_text.lower() and not re.search(
                rf"partida\s+às\s+{re.escape(trip_time)}(?!\d)", card_text, re.IGNORECASE
            ):
                return False, f"A partida do cartão localizado não corresponde às {trip_time}.", None
            if not locator_visible(select_outbound, 4000):
                return False, "Botão 'Selecionar ida' não está visível no comboio pretendido.", None

            print("[IDA] A selecionar o comboio pretendido.")
            select_outbound.click(timeout=10000)

            try:
                page.get_by_text("Remover seleção", exact=True).last.wait_for(
                    state="visible",
                    timeout=12000,
                )
            except PlaywrightTimeoutError:
                return False, "A CP não confirmou a seleção do comboio.", None

            print("[IDA] Seleção confirmada.")
            page.wait_for_timeout(800)

            if not dismiss_blocking_modal(page):
                return False, "Existe um diálogo que impede a continuação segura.", None

            # -----------------------------------------------------------------
            # C. Abrir o processo de compra
            # -----------------------------------------------------------------
            buy_button = find_visible_button(page, ["Comprar"])
            if buy_button is None:
                # Alguns elementos do portal podem não expor role=button.
                buy_button = page.get_by_text("Comprar", exact=True).last
                if not locator_visible(buy_button, 5000):
                    return False, "Botão 'Comprar' não encontrado.", None

            buy_button.scroll_into_view_if_needed()
            print("[COMPRA] A abrir o processo de compra.")
            buy_button.click(timeout=10000)

            try:
                page.wait_for_load_state("domcontentloaded", timeout=12000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1800)

            body_text = page.locator("body").inner_text(timeout=7000)
            if "Não foi possível iniciar o processo de compra" in body_text:
                return False, "A CP indicou que não foi possível iniciar o processo de compra.", None

            # O portal CP apresenta primeiro um ecrã-resumo, com declarações
            # obrigatórias e o botão "Avançar para compra". Só depois desse
            # passo é disponibilizado o formulário do passageiro.
            step_ok, step_details = prepare_intermediate_purchase_step(page)
            if not step_ok:
                return False, step_details, None


            # -----------------------------------------------------------------
            # D. Dados do passageiro e Passe Ferroviário Verde
            # -----------------------------------------------------------------
            name_selectors = [
                "input[name*='nome' i]",
                "input[placeholder*='Nome' i]",
                "input[id*='nome' i]",
            ]
            email_selectors = [
                "input[type='email']",
                "input[name*='email' i]",
                "input[placeholder*='Email' i]",
            ]

            name_locator = first_existing(page, name_selectors)
            if name_locator is None or not locator_visible(name_locator, 15000):
                latest_body = " ".join(page.locator("body").inner_text(timeout=5000).split())
                return (
                    False,
                    f"Formulário do passageiro não encontrado após avançar. URL: {page.url}. "
                    f"Ecrã: {latest_body[-1100:]}",
                    None,
                )

            print("[PASSAGEIROS] Formulário detetado.")
            clean_name = " ".join(passenger_name.split()).strip()
            if len(clean_name.split()) < 2:
                print(
                    "[AVISO PASSAGEIRO] PASSENGER_NAME deve conter pelo menos primeiro e último nome "
                    f"(valor atual: '{clean_name}')."
                )
            if len(clean_name) > 50:
                print(
                    f"[AVISO PASSAGEIRO] PASSENGER_NAME tem {len(clean_name)} caracteres "
                    "(máximo recomendado: 50)."
                )

            name_input = find_name_input(page)
            if name_input is None:
                return False, "Não foi possível localizar o campo 'Nome e apelido' do passageiro.", None

            try:
                name_input.scroll_into_view_if_needed(timeout=3000)
                name_input.click(timeout=4000)
                name_input.fill(clean_name, timeout=5000)
                name_input.dispatch_event("input")
                name_input.dispatch_event("change")
                name_input.press("Tab")
                page.wait_for_timeout(300)
                actual_name = " ".join((name_input.input_value() or "").split()).strip()
                if not actual_name:
                    return False, "O campo 'Nome e apelido' continuou vazio após preenchimento.", None
                print(f"[PASSAGEIRO] Nome preenchido e validado: '{actual_name}'.")
            except Exception as exc:
                return False, f"Não foi possível preencher o nome do passageiro: {exc}", None

            
            if not fill_required_field(page, email_selectors, passenger_email, "E-mail"):
                return False, "Não foi possível preencher o e-mail do passageiro.", None

            doc_trigger = page.locator("text=/Selecione o documento/i").first
            if doc_trigger.count() > 0 and locator_visible(doc_trigger, 1500):
                try:
                    doc_trigger.click(timeout=4000)
                    page.wait_for_timeout(600)
                    doc_option = None
                    for opt_regex in (
                        re.compile(r"^\s*Cart[aã]o\s+de\s+Cidad[aã]o\s*$", re.IGNORECASE),
                        re.compile(r"^\s*(?:Passaporte|T[ií]tulo\s+de\s+Resid[eê]ncia|Outro)\s*$", re.IGNORECASE),
                    ):
                        cands = page.locator(
                            "[role='option']:visible, li:visible, button:visible, div:visible, span:visible"
                        ).filter(has_text=opt_regex)
                        for d_idx in range(cands.count()):
                            cand = cands.nth(d_idx)
                            txt = " ".join((cand.inner_text() or "").split())
                            if txt and len(txt) <= 45 and opt_regex.match(txt) and locator_visible(cand, 600):
                                doc_option = cand
                                break
                        if doc_option is not None:
                            break
                    if doc_option is not None:
                        doc_label = " ".join((doc_option.inner_text() or "").split())
                        doc_option.click(timeout=4000)
                        page.wait_for_timeout(500)
                        print(f"[DOC] Tipo de documento selecionado: {doc_label}")
                except Exception as exc:
                    print(f"[AVISO DOC] Não foi possível selecionar o tipo de documento: {exc}")

            id_filled = False
            try:
                visible_inputs = page.locator("input:visible")
                for idx in range(visible_inputs.count()):
                    inp = visible_inputs.nth(idx)
                    inp_type = (inp.get_attribute("type") or "").lower()
                    if inp_type in ("text", "tel", "number", ""):
                        curr_val = (inp.input_value() or "").strip()
                        if not curr_val:
                            inp.fill(passenger_id, timeout=5000)
                            id_filled = True
                            print(f"[DOC] Número de identificação preenchido no campo visível #{idx}.")
                            break
            except Exception as exc:
                print(f"[AVISO DOC] Falha ao preencher número de identificação: {exc}")

            if not id_filled:
                return False, "Não foi possível preencher o documento de identificação.", None

            discount_selected = False
            try:
                discount_triggers = [
                    page.locator("[role='combobox']:visible").filter(
                        has_text=re.compile(r"inteiro|desconto|tarifa|verde", re.IGNORECASE)
                    ),
                    page.locator("button:visible").filter(
                        has_text=re.compile(r"inteiro|desconto|tarifa|verde", re.IGNORECASE)
                    ),
                    page.locator("text=/^\\s*Inteiro\\s*$/i"),
                ]

                for trig_group in discount_triggers:
                    for t_idx in range(min(trig_group.count(), 4)):
                        trig = trig_group.nth(t_idx)
                        if not locator_visible(trig, 800):
                            continue
                        trig.scroll_into_view_if_needed(timeout=3000)
                        trig.click(timeout=4000, force=True)
                        page.wait_for_timeout(700)

                        cands = page.locator(
                            "[role='option']:visible, [role='menuitem']:visible, li:visible, button:visible, div:visible, span:visible"
                        )
                        for c_idx in range(min(cands.count(), 160)):
                            cand = cands.nth(c_idx)
                            try:
                                txt = " ".join((cand.inner_text() or "").split())
                                if not txt or len(txt) > 100:
                                    continue
                                norm_txt = _normalize_text(txt)
                                if "ferroviario" in norm_txt and "verde" in norm_txt:
                                    cand.click(timeout=4000, force=True)
                                    page.wait_for_timeout(800)
                                    print(f"[DESCONTO] Selecionado: {txt}")
                                    discount_selected = True
                                    break
                            except Exception:
                                continue

                        if discount_selected:
                            break
                    if discount_selected:
                        break

                if not discount_selected:
                    discount_selected = select_green_pass(page)
            except Exception as exc:
                print(f"[AVISO DESCONTO] Erro ao abrir/selecionar desconto: {exc}")


            if not discount_selected:
                return False, "Não foi possível selecionar Passe Ferroviário Verde.", None

            
            page.wait_for_timeout(500)
            extra_info = None

            # Procurar primeiro qualquer campo obrigatório visível que ainda esteja vazio
            req_inputs = page.locator("input:visible[required]")
            for idx in range(req_inputs.count()):
                cand = req_inputs.nth(idx)
                if not (cand.input_value() or "").strip():
                    extra_info = cand
                    break

            if extra_info is None:
                extra_info = find_additional_info_input(page)

            if extra_info is not None:
                try:
                    extra_info.scroll_into_view_if_needed(timeout=3000)
                    extra_info.click(timeout=4000)
                    extra_info.fill(pass_code, timeout=5000)
                    extra_info.dispatch_event("input")
                    extra_info.dispatch_event("change")
                    extra_info.press("Tab")
                    page.wait_for_timeout(300)
                    print("[PASSE] Campo 'Informação adicional' preenchido com CP_PASS_CODE.")
                except Exception as exc:
                    return False, f"Não foi possível preencher o código do passe: {exc}", None
            else:
                curr_body = " ".join((page.locator("body").inner_text(timeout=3000) or "").split())
                if re.search(r"informa[çc][aã]o\s+adicional", curr_body, re.IGNORECASE):
                    return (
                        False,
                        "O campo 'Informação adicional' aparece no ecrã, mas o respetivo <input> não foi localizado.",
                        None,
                    )
                print("[PASSE] Campo 'Informação adicional' não presente neste ecrã; a prosseguir.")

            
            # Garantir clique no botão 'Avançar' do formulário de passageiros
            advance_passenger_btn = find_visible_button(page, ["Avançar", "Continuar", "Seguinte"])
            if advance_passenger_btn is None:
                return False, "Botão 'Avançar' do formulário de passageiros não encontrado.", None

            before_text = " ".join((page.locator("body").inner_text(timeout=3000) or "").split())
            before_url = page.url

            try:
                advance_passenger_btn.scroll_into_view_if_needed(timeout=3000)
                if not advance_passenger_btn.is_enabled(timeout=8000):
                    vis_inputs = page.locator("input:visible")
                    states = []
                    for i in range(vis_inputs.count()):
                        el = vis_inputs.nth(i)
                        el_id = el.get_attribute("id") or el.get_attribute("name") or f"#{i}"
                        el_type = el.get_attribute("type") or "text"
                        has_val = bool((el.input_value() or "").strip())
                        invalid = el.get_attribute("aria-invalid") or ""
                        req = el.get_attribute("required")
                        states.append(
                            f"{el_id}(type={el_type},filled={has_val},req={req is not None},invalid={invalid})"
                        )
                    print(f"[DEBUG INPUTS PASSAGEIRO] {', '.join(states)}")
                    debug_txt = " ".join((page.locator("body").inner_text(timeout=3000) or "").split())
                    print(f"[DEBUG AVANÇAR DISABLED] {debug_txt[-900:]}")
                    return False, "Botão 'Avançar' permaneceu desativado após preencher os dados do passageiro.", None

                print("[PASSAGEIROS] A clicar em 'Avançar' para prosseguir.")
                advance_passenger_btn.click(timeout=8000)
                page.wait_for_timeout(800)
            except PlaywrightTimeoutError:
                debug_txt = " ".join((page.locator("body").inner_text(timeout=3000) or "").split())
                print(f"[DEBUG AVANÇAR TIMEOUT] {debug_txt[-900:]}")
                return False, "Timeout ao clicar em 'Avançar' no formulário de passageiros.", None

            transitioned = False
            deadline = time.monotonic() + 25
            last_curr_text = before_text

            while time.monotonic() < deadline:
                try:
                    curr_text = " ".join((page.locator("body").inner_text(timeout=3000) or "").split())
                except Exception:
                    page.wait_for_timeout(600)
                    continue

                last_curr_text = curr_text

                if "Não foi possível iniciar o processo de compra" in curr_text:
                    return False, "A CP rejeitou a validação do passe ou não existem lugares elegíveis.", None

                still_passenger_form = (
                    "Dados dos passageiros" in curr_text
                    and re.search(r"informa[çc][aã]o\s+adicional", curr_text, re.IGNORECASE) is not None
                )
                reached_next_step = (
                    re.search(
                        r"\b2\s+Pagamento\b|Escolha\s+o\s+lugar|Sele[çc][aã]o\s+de\s+lugar|Resumo\s+da\s+compra|Total\s+a\s+pagar",
                        curr_text,
                        re.IGNORECASE,
                    )
                    is not None
                )

                if "Por favor aguarde um momento" not in curr_text and (
                    reached_next_step
                    or (not still_passenger_form and curr_text != before_text)
                    or page.url != before_url
                ):
                    transitioned = True
                    break

                page.wait_for_timeout(700)

            if not transitioned:
                print(f"[DEBUG ERRO AVANÇAR] URL={page.url} | {last_curr_text[-900:]}")
                return (
                    False,
                    "O formulário de passageiros não avançou após clicar em 'Avançar' "
                    "(verificar validação do passe/campos no ecrã).",
                    None,
                )



            # -----------------------------------------------------------------
            # E. Carruagem/lugar e resumo
            # -----------------------------------------------------------------
            seat_summary = select_coach_and_seat(page)
            next_button = find_visible_button(page, ["Continuar", "Avançar", "Confirmar", "Seguinte"])
            if next_button is not None:
                try:
                    next_button.click(timeout=7000)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except PlaywrightTimeoutError:
                        pass
                    page.wait_for_timeout(1200)
                except Exception as exc:
                    print(f"[AVISO LUGAR] Não foi possível avançar após a seleção de lugar: {exc}")
                    
            # -----------------------------------------------------------------
            # F. Circuit breaker: só prosseguir se o total explícito for 0,00 €
            # -----------------------------------------------------------------
            if not verify_zero_euro_total(page):
                return False, "ABORTADO POR SEGURANÇA: total de 0,00 € não confirmado.", None

            # Termos podem ser apresentados como checkbox. Se existir, tem de
            # ficar selecionado. A ausência é registada, mas não se seleciona
            # qualquer checkbox de publicidade por engano.
            terms_found = accept_terms_if_present(page)
            if not terms_found:
                # Alguns fluxos da CP não mostram checkbox nesta etapa; só
                # prosseguir se não houver checkbox visível por selecionar.
                visible_checkboxes = page.locator("input[type='checkbox']:visible")
                if visible_checkboxes.count() > 0:
                    return False, "Existe um checkbox visível de termos que não foi possível confirmar.", None

            # Revalidar o total imediatamente antes de submeter.
            if not verify_zero_euro_total(page):
                return False, "ABORTADO POR SEGURANÇA: o total deixou de ser 0,00 €.", None

            final_button = find_visible_button(
                page,
                ["Concluir", "Finalizar", "Confirmar compra", "Emitir", "Concluir compra"],
            )
            if final_button is None:
                return False, "Botão final de emissão/conclusão não encontrado.", None

            print("[EMISSÃO] A submeter a compra com total confirmado de 0,00 €.")
            # A partir daqui, qualquer falha é tratada como estado incerto para
            # impedir uma segunda tentativa automática que possa duplicar a compra.
            final_submit_attempted = True
            final_button.click(timeout=10000)

            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1800)

            final_body = page.locator("body").inner_text(timeout=8000)
            final_body_normalized = " ".join(final_body.split())
            print(f"[DEBUG CONFIRMAÇÃO] {final_body_normalized[-1600:]}")

            success_pattern = re.compile(
                r"compra\s+(?:foi\s+)?(?:efetuada|conclu[ií]da|confirmada)\s+com\s+sucesso|"
                r"bilhete\s+(?:foi\s+)?emitido|"
                r"reserva\s+confirmada|"
                r"pedido\s+confirmado|"
                r"compra\s+confirmada|"
                r"obrigado.{0,80}(?:compra|reserva|bilhete)",
                re.IGNORECASE,
            )

            pdf_bytes = download_ticket_pdf(page)
            if not success_pattern.search(final_body_normalized) and not pdf_bytes:
                return (
                    False,
                    "[REVIEW_REQUIRED] A submissão final foi tentada, mas a página não confirmou a emissão. "
                    "Verificar manualmente a área de reservas/e-mail antes de permitir nova tentativa.",
                    None,
                )

            return True, seat_summary, pdf_bytes

        except PlaywrightTimeoutError as exc:
            if final_submit_attempted:
                return (
                    False,
                    f"[REVIEW_REQUIRED] Timeout depois de tentar submeter a compra: {exc}. "
                    "Verificar manualmente antes de repetir.",
                    None,
                )
            return False, f"Timeout durante a navegação/interação: {exc}", None
        except Exception as exc:
            if final_submit_attempted:
                return (
                    False,
                    f"[REVIEW_REQUIRED] Erro depois de tentar submeter a compra: {type(exc).__name__}: {exc}. "
                    "Verificar manualmente antes de repetir.",
                    None,
                )
            return False, f"Erro inesperado durante a tentativa: {type(exc).__name__}: {exc}", None
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


# =============================================================================
# 4. ORQUESTRADOR / MÁQUINA DE ESTADOS
# =============================================================================

def main() -> None:
    if os.getenv("RESET_STATUS", "false").strip().lower() == "true":
        update_github_variable("BOOKING_STATUS", "PENDING")
        status = "PENDING"
    else:
        status = os.getenv("BOOKING_STATUS", "PENDING").strip().upper()

    if status != "PENDING":
        print(f"[STOP] Estado atual: {status}. Nenhuma ação executada.")
        return

    origin = os.getenv("TRIP_ORIGIN", "").strip()
    destination = os.getenv("TRIP_DESTINATION", "").strip()
    trip_date = os.getenv("TRIP_DATE", "").strip()
    trip_time = os.getenv("TRIP_TIME", "").strip()

    if not all((origin, destination, trip_date, trip_time)):
        print(
            "[ERRO CONFIG] Faltam variáveis obrigatórias: "
            "TRIP_ORIGIN, TRIP_DESTINATION, TRIP_DATE e TRIP_TIME."
        )
        return

    try:
        departure_naive = datetime.strptime(
            f"{trip_date} {trip_time}",
            "%Y-%m-%d %H:%M",
        )
        departure_dt = departure_naive.replace(tzinfo=LISBON_TZ)
    except ValueError:
        print("[ERRO CONFIG] TRIP_DATE/TRIP_TIME devem usar AAAA-MM-DD e HH:MM.")
        return

    now = datetime.now(LISBON_TZ)

    # Início 10 minutos antes para permitir abrir o portal; tentativa exata na
    # abertura prevista (24 horas antes da partida); manter tentativas durante
    # 3 horas após a abertura, tal como definido pela janela abaixo.
    window_pre_start = departure_dt - timedelta(hours=24, minutes=10)
    window_open_exact = departure_dt - timedelta(hours=24) + timedelta(seconds=1)
    window_end = departure_dt - timedelta(hours=21)

    print(
        f"[TEMPO] Agora: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
        f"Partida: {departure_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    if now < window_pre_start:
        print(
            f"[AGUARDAR] Fora da janela ativa. Próxima execução útil a partir de "
            f"{window_pre_start.strftime('%Y-%m-%d %H:%M:%S %Z')} Europe/Lisbon."
        )
        return

    if now > window_end:
        print("[EXPIRADO] Terminou a janela de 3 horas após a abertura sem emissão confirmada.")
        update_github_variable("BOOKING_STATUS", "FAILED_EXPIRED")
        send_email(
            subject="Não foi possível emitir o bilhete CP",
            body=(
                "Não foi possível emitir o bilhete dentro da janela de tentativas.\n\n"
                f"Trajeto: {origin} -> {destination}\n"
                f"Data: {trip_date} às {trip_time}\n"
                "Serviço pretendido: Intercidades, 2.ª classe\n"
                "Estado: FAILED_EXPIRED\n"
            ),
        )
        return

    wait_target = window_open_exact if now < window_open_exact else None
    success, details, pdf_bytes = attempt_ticket_purchase(
        origin=origin,
        destination=destination,
        trip_date=trip_date,
        trip_time=trip_time,
        wait_until_dt=wait_target,
    )

    if success:
        print(f"[SUCESSO] Emissão confirmada. {details}")
        if update_github_variable("BOOKING_STATUS", "BOUGHT"):
            print("[ESTADO] BOOKING_STATUS definido como BOUGHT.")
        else:
            print("[AVISO] Bilhete emitido, mas não foi possível atualizar BOOKING_STATUS no GitHub.")

        send_email(
            subject="Bilhete CP emitido",
            body=(
                "Bilhete CP emitido com sucesso.\n\n"
                "Detalhes: total validado de 0,00 € (Passe Ferroviário Verde).\n"
                f"Trajeto: {origin} -> {destination}\n"
                f"Data/hora: {trip_date} às {trip_time}\n"
                f"Alocação: {details}\n"
            ),
            pdf_bytes=pdf_bytes,
            pdf_filename=f"Bilhete_CP_{trip_date}_{trip_time.replace(':', 'h')}.pdf",
        )
    else:
        if details.startswith("[REVIEW_REQUIRED]"):
            print(f"[REVISÃO MANUAL] {details}")
            update_github_variable("BOOKING_STATUS", "REVIEW_REQUIRED")
            send_email(
                subject="Verificação manual necessária — compra CP",
                body=(
                    "O processo chegou à submissão final, mas não foi possível confirmar o resultado. "
                    "Para evitar uma eventual compra duplicada, as tentativas automáticas foram suspensas.\n\n"
                    f"Trajeto: {origin} -> {destination}\n"
                    f"Data/hora: {trip_date} às {trip_time}\n\n"
                    f"Detalhe: {details}\n\n"
                    "Verifique a área de reservas da CP e o e-mail antes de repor BOOKING_STATUS para PENDING."
                ),
            )
        else:
            # Antes da submissão final, uma falha é segura para nova tentativa.
            print(f"[TENTATIVA SEM SUCESSO] {details}. Será tentado novamente no próximo ciclo.")


if __name__ == "__main__":
    main()
