import os
import re
import time
import smtplib
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

LISBON_TZ = ZoneInfo("Europe/Lisbon")


# ==============================================================================
# 1. GESTÃO DE ESTADO (GITHUB REPOSITORY VARIABLES) E NOTIFICAÇÕES (SMTP)
# ==============================================================================

def update_github_variable(var_name: str, value: str) -> None:
    token = os.getenv("GH_PAT")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        print(f"[AVISO] GH_PAT ou GITHUB_REPOSITORY em falta. Não foi possível gravar {var_name}={value}.")
        return

    url = f"https://api.github.com/repos/{repo}/actions/variables/{var_name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.patch(url, headers=headers, json={"name": var_name, "value": value}, timeout=15)
    if resp.status_code == 404:
        create_url = f"https://api.github.com/repos/{repo}/actions/variables"
        resp = requests.post(create_url, headers=headers, json={"name": var_name, "value": value}, timeout=15)

    if resp.status_code in (200, 201, 204):
        print(f"[ESTADO] Variável {var_name} atualizada para: {value}")
    else:
        print(f"[ERRO API GITHUB] Falha ao atualizar {var_name}: {resp.status_code} - {resp.text}")


def send_email(subject: str, body: str, pdf_bytes: bytes | None = None, pdf_filename: str = "bilhete_cp.pdf") -> None:
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    notify_email = os.getenv("NOTIFY_EMAIL")

    if not all([smtp_user, smtp_pass, notify_email]):
        print("[AVISO] Credenciais SMTP incompletas. E-mail não enviado.")
        return

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = notify_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if pdf_bytes:
        attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
        attachment.add_header("Content-Disposition", "attachment", filename=pdf_filename)
        msg.attach(attachment)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"[EMAIL] Enviado com sucesso para {notify_email} | Assunto: {subject}")
    except Exception as e:
        print(f"[ERRO EMAIL] Falha ao enviar e-mail: {e}")


# ==============================================================================
# 2. LÓGICA DE NAVEGAÇÃO E COMPRA NO SITE DA CP
# ==============================================================================

def handle_cookies(page) -> None:
    
def dismiss_blocking_modal(page) -> bool:
    """Deteta e tenta fechar um modal que bloqueia a página."""

    modal = page.locator(
        '[role="dialog"][aria-modal="true"]:visible, '
        '.modal-overlay:visible'
    ).last

    if modal.count() == 0:
        return True

    try:
        modal_text = modal.inner_text(timeout=3000)
    except Exception:
        modal_text = "Texto do modal indisponível"

    print(f"[MODAL] Modal detetado: {modal_text[:1000]}")

    buttons = modal.locator(
        "button:visible, [role='button']:visible"
    )

    button_names = []
    for i in range(buttons.count()):
        try:
            button_names.append(buttons.nth(i).inner_text().strip())
        except Exception:
            pass

    print(f"[MODAL] Botões disponíveis: {button_names}")

    # Procurar um botão explícito de fechar
    close_button = modal.locator(
        "button[aria-label*='fechar' i], "
        "button[title*='fechar' i], "
        "button[aria-label*='close' i], "
        "button[title*='close' i], "
        "button.close, "
        "[data-bs-dismiss='modal'], "
        "[data-dismiss='modal'], "
        ".modal-close, "
        ".close-modal"
    ).first

    if close_button.count() > 0:
        try:
            if close_button.is_visible():
                close_button.click(timeout=3000)
                modal.wait_for(state="hidden", timeout=4000)
                print("[MODAL] Modal fechado com sucesso.")
                return True
        except Exception as e:
            print(f"[MODAL] Falha ao clicar no botão fechar: {e}")

    # Procurar botão com texto explicitamente indicativo de fechar
    close_text = modal.get_by_role(
        "button",
        name=re.compile(r"^(fechar|close|dispensar)$", re.IGNORECASE)
    ).first

    if close_text.count() > 0:
        try:
            close_text.click(timeout=3000)
            modal.wait_for(state="hidden", timeout=4000)
            print("[MODAL] Modal fechado através do botão.")
            return True
        except Exception:
            pass

    # Última tentativa: tecla Escape
    try:
        page.keyboard.press("Escape")
        modal.wait_for(state="hidden", timeout=3000)
    except Exception:
        pass

    if modal.is_visible():
        print(
            "[ERRO MODAL] Modal continua ativo. "
            "Não será efetuado nenhum clique por baixo do modal."
        )
        return False

    print("[MODAL] Modal removido.")
    return True

    try:
        cookie_btn = page.locator("#onetrust-reject-all-handler, #onetrust-accept-btn-handler").first
        if cookie_btn.is_visible(timeout=4000):
            cookie_btn.click()
            page.wait_for_timeout(600)
    except Exception:
        pass


def fill_station_input(page, input_locator, station_name: str) -> bool:
    """Preenche a estação e garante que uma opção da lista suspensa é clicada (até 2 tentativas)."""
    for attempt in range(2):
        input_locator.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        input_locator.press_sequentially(station_name, delay=90)
        page.wait_for_timeout(1800)

        suggestion = page.locator(
            "[role='option'], .ui-autocomplete li, [class*='autocomplete'] li, [class*='suggestion'], [class*='dropdown'] li"
        ).filter(has_text=re.compile(re.escape(station_name), re.IGNORECASE)).first

        if suggestion.is_visible(timeout=3500):
            suggestion.click()
            page.wait_for_timeout(500)
            print(f"[FORM] Estação selecionada na lista: {station_name}")
            return True

        print(f"[AVISO FORM] Tentativa {attempt + 1}: lista não apareceu para '{station_name}', a tentar novamente...")

    return False


def select_coach_and_seat(page) -> str:
    info_lugar = "Lugar aleatório atribuído pelo sistema"
    try:
        coach_elements = page.locator("[data-coach], .coach-selector button, .carruagem-item, select[name*='coach'] option")
        count = coach_elements.count()

        if count > 0:
            selected_coach = None
            for target_coach in ["24", "23"]:
                candidate = page.locator(
                    f"[data-coach='{target_coach}'], button:has-text('{target_coach}'), .carruagem-item:has-text('{target_coach}')"
                ).first
                if candidate.count() > 0 and candidate.is_enabled():
                    candidate.click()
                    selected_coach = target_coach
                    page.wait_for_timeout(1000)
                    break

            if not selected_coach:
                last_coach = coach_elements.last
                selected_coach = last_coach.inner_text().strip()
                last_coach.click()
                page.wait_for_timeout(1000)

            for target_seat in ["68", "63"]:
                seat_locator = page.locator(
                    f"[data-seat='{target_seat}']:not(.occupied):not(.unavailable):not([disabled]), "
                    f".seat.available:has-text('{target_seat}'), "
                    f"button.lugar-livre:has-text('{target_seat}')"
                ).first
                if seat_locator.count() > 0 and seat_locator.is_visible():
                    seat_locator.click()
                    info_lugar = f"Carruagem {selected_coach}, Lugar {target_seat}"
                    print(f"[LUGAR] Selecionado com sucesso: {info_lugar}")
                    return info_lugar

            info_lugar = f"Carruagem {selected_coach} (lugares 68/63 indisponíveis; mantido lugar automático)"
    except Exception as e:
        print(f"[AVISO LUGAR] Não foi possível personalizar lugar ({e}). A avançar com lugar automático.")

    return info_lugar


def verify_zero_euro_total(page) -> bool:
    page_text = page.locator("body").inner_text()
    matches = re.findall(r"(?:Total|A pagar|Valor total)[^\d€]{0,25}(\d+[.,]\d{2})\s*€", page_text, re.IGNORECASE)
    if not matches:
        matches = re.findall(r"(\d+[.,]\d{2})\s*€", page_text)

    if not matches:
        print("[SEGURANÇA] Não foi possível confirmar o valor total no ecrã. Abortar por precaução.")
        return False

    final_amount = matches[-1].replace(",", ".")
    print(f"[SEGURANÇA] Valor total detetado: {final_amount} €")
    return float(final_amount) == 0.0


def find_specific_train_card(page, trip_time: str):
    """
    Atravessa o Shadow DOM do novo portal da CP partindo diretamente do nó de texto `trip_time`
    e sobe na árvore DOM até encontrar o cartão do comboio com o botão de seleção.
    """
    time_node = page.locator(f"text='{trip_time}'").first
    try:
        time_node.wait_for(state="visible", timeout=15000)
        page.wait_for_timeout(1500)
    except Exception:
        print(f"[DEBUG] A hora '{trip_time}' não ficou visível na página após 15s.")
        return None

    # Sobe níveis na árvore DOM a partir do texto '19:45' até encontrar o cartão completo do comboio
    for level in range(1, 7):
        ancestor = time_node.locator(f"xpath={'../' * level}.").first
        try:
            txt = ancestor.inner_text()
            # O cartão individual de um comboio tem entre 30 e 1200 caracteres e menciona IC / Intercidades
            if 30 <= len(txt) <= 1200 and re.search(r"Intercidades|\bIC\b", txt, re.IGNORECASE):
                print(f"[DEBUG] Cartão encontrado no nível {level} acima de '{trip_time}'.")
                return ancestor
        except Exception:
            continue

    # Fallback: devolve o contentor 3 níveis acima da hora para clicar no cartão/botão
    print(f"[DEBUG] A devolver contentor pai direto (nível 3) de '{trip_time}'.")
    return time_node.locator("xpath=../../..").first


def attempt_ticket_purchase(
    origin: str,
    destination: str,
    trip_date: str,
    trip_time: str,
    wait_until_dt: datetime | None = None,
) -> tuple[bool, str, bytes | None]:
    pass_code = os.environ["CP_PASS_CODE"]
    passenger_name = os.environ["PASSENGER_NAME"]
    passenger_id = os.environ["PASSENGER_ID_NUMBER"]
    passenger_email = os.environ["PASSENGER_EMAIL"]

    dt_obj = datetime.strptime(trip_date, "%Y-%m-%d")
    cp_date_str = dt_obj.strftime("%d/%m/%Y")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="pt-PT",
            timezone_id="Europe/Lisbon",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            print(f"[NAVEGAÇÃO] A aceder a https://cp.pt/pt/ para {origin} -> {destination} ({cp_date_str} às {trip_time})")
            page.goto("https://cp.pt/pt/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            handle_cookies(page)

            # 1. Preencher formulário de pesquisa
            text_inputs = page.locator("input[type='text']:visible")
            origin_input = page.locator("input[placeholder*='Origem' i], input[aria-label*='Origem' i], input[name*='origem' i]").first
            dest_input = page.locator("input[placeholder*='Destino' i], input[aria-label*='Destino' i], input[name*='destino' i]").first

            if origin_input.count() == 0 and text_inputs.count() >= 2:
                origin_input = text_inputs.nth(0)
                dest_input = text_inputs.nth(1)

            ok_origin = fill_station_input(page, origin_input, origin)
            ok_dest = fill_station_input(page, dest_input, destination)

            if not (ok_origin and ok_dest):
                return False, f"Falha ao selecionar estações na lista (origem={ok_origin}, destino={ok_dest}).", None

            date_input = page.locator(
                "input[placeholder*='Data' i], input[aria-label*='Data' i], input[name*='data' i], input[placeholder*='dd/mm' i]"
            ).first
            if date_input.count() == 0 and text_inputs.count() >= 3:
                date_input = text_inputs.nth(2)

            if date_input.count() > 0:
                date_input.click()
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                date_input.press_sequentially(cp_date_str, delay=70)
                page.keyboard.press("Tab")
                print(f"[FORM] Data preenchida: {cp_date_str}")

            if wait_until_dt:
                now = datetime.now(LISBON_TZ)
                seconds_to_wait = (wait_until_dt - now).total_seconds()
                if 0 < seconds_to_wait <= 660:
                    print(f"[SYNC] A aguardar {seconds_to_wait:.2f}s até à abertura exata ({wait_until_dt.strftime('%H:%M:%S')})...")
                    time.sleep(seconds_to_wait)

            search_btn = page.locator("button.search-button, button:has-text('Pesquisar')").first
            if search_btn.is_disabled():
                return False, "Botão 'Pesquisar' permaneceu desativado após preenchimento.", None

            search_btn.click()
            page.wait_for_timeout(4000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            print(f"[DEBUG URL] URL após pesquisa: {page.url}")


            # 2. Selecionar comboio e avançar para a compra

            train_card = find_specific_train_card(page, trip_time)

            if not train_card:
                return False, (
                    f"Comboio Intercidades das {trip_time} não encontrado."
                ), None

            row_container = train_card.locator("xpath=../..").first

            if row_container.count() > 0:
                if len(row_container.inner_text()) < 1500:
                    train_card = row_container

            print(
                f"[DEBUG COMBOIO] "
                f"{train_card.inner_text().replace(chr(10), ' | ')}"
            )

            select_ida = train_card.locator(
                "button:visible, a:visible, [role='button']:visible"
            ).filter(
                has_text=re.compile(r"Selecionar ida", re.IGNORECASE)
            ).first

            if select_ida.count() == 0:
                return False, "Botão Selecionar ida não encontrado.", None

            print("[IDA] A selecionar o comboio.")
            select_ida.click(timeout=10000)

            try:
                page.get_by_text(
                    "Remover seleção",
                    exact=True
                ).last.wait_for(state="visible", timeout=10000)

                print("[IDA] Seleção confirmada.")

            except PlaywrightTimeoutError:
                return False, (
                    "A CP não confirmou a seleção do comboio."
                ), None

            page.wait_for_timeout(1000)

            # Fechar eventuais modais que bloqueiem a compra
            if not dismiss_blocking_modal(page):
                return False, (
                    "Modal da CP impede a interação com o botão Comprar."
                ), None

            # Procurar o botão Comprar
            buy_pattern = re.compile(
                r"^\s*Comprar\s*$",
                re.IGNORECASE
            )

            buy_button = page.get_by_role(
                "button",
                name=buy_pattern
            ).last

            if buy_button.count() == 0:
                buy_button = page.get_by_role(
                    "link",
                    name=buy_pattern
                ).last

            if buy_button.count() == 0:
                buy_button = page.get_by_text(
                    "Comprar",
                    exact=True
                ).last

            try:
                buy_button.wait_for(
                    state="visible",
                    timeout=10000
                )
            except PlaywrightTimeoutError:
                return False, "Botão Comprar não encontrado.", None

            # Confirmar novamente que não existe um modal por cima
            if not dismiss_blocking_modal(page):
                return False, (
                    "Modal continua a bloquear o botão Comprar."
                ), None

            buy_button.scroll_into_view_if_needed()

            print("[COMPRA] A clicar no botão Comprar.")

            try:
                buy_button.click(timeout=8000)
            except Exception as e:
                return False, (
                    f"Não foi possível clicar em Comprar: {e}"
                ), None

            page.wait_for_timeout(3500)

            try:
                page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=10000
                )
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(1500)

            print(f"[DEBUG URL COMPRA] {page.url}")

            body_text = page.locator("body").inner_text()

            print(
                f"[DEBUG ECRÃ COMPRA] "
                f"{body_text[:2000].replace(chr(10), ' | ')}"
            )

            visible_inputs = page.locator(
                "input:visible, select:visible, textarea:visible"
            )

            print(
                f"[DEBUG PASSAGEIROS] "
                f"Inputs/Selects visíveis: {visible_inputs.count()}"
            )

            if visible_inputs.count() == 0:
                return False, (
                    "Formulário do passageiro não apresentado. "
                    f"URL: {page.url} | "
                    f"Ecrã: {body_text[-1500:]}"
                ), None

            print("[PASSAGEIROS] Formulário detetado.")


            
            # 3. Preencher dados do passageiro e Passe Ferroviário Verde


            print("[PASSAGEIROS] Formulário detetado.")


            page.wait_for_timeout(1500)

            # Procura botão de avançar / comprar (pode aparecer só após selecionar a linha/tarifa)
            continue_btn = page.locator(
                "button:visible:has-text('Continuar'), button:visible:has-text('Avançar'), button:visible:has-text('Comprar'), button:visible:has-text('Seguinte'), button:visible:has-text('Reservar')"
            ).first
            if continue_btn.count() > 0:
                print(f"[AVANÇAR] A clicar em '{continue_btn.inner_text().strip()}'...")
                continue_btn.click()
                page.wait_for_timeout(3000)

            print(f"[DEBUG URL PASSAGEIROS] URL atual: {page.url}")

            body_text = page.locator("body").inner_text()
            if "Não foi possível iniciar o processo de compra" in body_text:
                return False, "Mensagem detetada: 'Não foi possível iniciar o processo de compra'.", None

            # 3. Preencher dados do passageiro e Passe Ferroviário Verde


            visible_inputs = page.locator("input:visible, select:visible")

            if visible_inputs.count() == 0:
                return (
                    False,
                    "A página de dados do passageiro não foi apresentada "
                    f"após a seleção da ida. URL atual: {page.url}",
                    None
                )

            print(
                f"[DEBUG PASSAGEIROS] "
                f"Formulário detetado com {visible_inputs.count()} campos."
            )

            visible_inputs = page.locator("input:visible, select:visible")
            print(f"[DEBUG PASSAGEIROS] Inputs/Selects visíveis: {visible_inputs.count()}")

            name_field = page.locator("input[name*='nome' i], input[placeholder*='Nome' i]").first
            if name_field.is_visible(timeout=4000):
                name_field.fill(passenger_name)

            id_field = page.locator("input[name*='documento' i], input[name*='identificacao' i], input[placeholder*='Identificação' i], input[placeholder*='Cartão' i]").first
            if id_field.is_visible():
                id_field.fill(passenger_id)

            email_field = page.locator("input[type='email'], input[name*='email' i]").first
            if email_field.is_visible():
                email_field.fill(passenger_email)

            discount_select = page.locator("select[name*='desconto' i], select[id*='desconto' i]").first
            if discount_select.is_visible():
                discount_select.select_option(label="Passe Ferroviário Verde")
            else:
                discount_trigger = page.locator("button:has-text('Desconto'), button:has-text('Sem desconto'), [aria-label*='Desconto' i], label:has-text('Desconto')").first
                if discount_trigger.is_visible(timeout=5000):
                    discount_trigger.click()
                    page.wait_for_timeout(800)
                    page.locator("text=Passe Ferroviário Verde").first.click()
                else:
                    print(f"[DEBUG TEXTO ECRÃ] {body_text[:400].replace(chr(10), ' | ')}")
                    return False, "Campo 'Desconto' não encontrado no ecrã atual.", None

            page.wait_for_timeout(1000)

            extra_info_field = page.locator(
                "input[name*='adicional' i], input[placeholder*='Informação adicional' i], input[name*='cartao' i], input[name*='passe' i]"
            ).first
            extra_info_field.fill(pass_code)

            page.locator("button:has-text('Continuar'), button:has-text('Avançar'), button:has-text('Seguinte')").first.click()
            page.wait_for_timeout(2500)

            if "Não foi possível iniciar o processo de compra" in page.locator("body").inner_text():
                return False, "Erro ao validar Passe Ferroviário Verde / sem lugares disponíveis para o passe.", None

            # 4. Escolha de Carruagem (24/23) e Lugar (68/63)
            seat_summary = select_coach_and_seat(page)
            next_after_seat = page.locator("button:has-text('Continuar'), button:has-text('Avançar'), button:has-text('Confirmar'), button:has-text('Seguinte')").first
            if next_after_seat.is_visible():
                next_after_seat.click()
                page.wait_for_timeout(2000)

            # 5. Circuit Breaker: Validar Total == 0,00 €
            if not verify_zero_euro_total(page):
                return False, "ABORTADO POR SEGURANÇA: O total apresentado não é 0,00 €.", None

            # 6. Aceitar Termos e Concluir Compra
            terms_checkbox = page.locator("input[type='checkbox']").first
            if terms_checkbox.is_visible() and not terms_checkbox.is_checked():
                terms_checkbox.check()

            final_submit = page.locator("button:has-text('Concluir'), button:has-text('Finalizar'), button:has-text('Confirmar compra'), button:has-text('Emitir')").first
            final_submit.click()
            page.wait_for_load_state("networkidle", timeout=25000)

            # 7. Descarregar PDF do bilhete
            pdf_bytes = None
            download_btn = page.locator("a:has-text('PDF'), button:has-text('Descarregar'), a:has-text('Bilhete')").first
            if download_btn.is_visible(timeout=5000):
                try:
                    with page.expect_download(timeout=10000) as download_info:
                        download_btn.click()
                    download = download_info.value
                    pdf_path = download.path()
                    if pdf_path:
                        with open(pdf_path, "rb") as f:
                            pdf_bytes = f.read()
                except Exception as dl_err:
                    print(f"[AVISO PDF] Bilhete emitido, mas não foi possível descarregar o anexo PDF: {dl_err}")

            return True, seat_summary, pdf_bytes

        except PlaywrightTimeoutError as te:
            return False, f"Timeout durante a navegação: {te}", None
        except Exception as exc:
            return False, f"Erro inesperado na tentativa: {exc}", None
        finally:
            context.close()
            browser.close()


# ==============================================================================
# 3. ORQUESTRADOR PRINCIPAL (MÁQUINA DE ESTADOS)
# ==============================================================================

def main() -> None:
    if os.getenv("RESET_STATUS", "false").lower() == "true":
        update_github_variable("BOOKING_STATUS", "PENDING")
        status = "PENDING"
    else:
        status = os.getenv("BOOKING_STATUS", "PENDING").strip().upper()

    if status != "PENDING":
        print(f"[STOP] Estado atual é '{status}'. Nenhuma ação necessária.")
        return

    origin = os.getenv("TRIP_ORIGIN", "").strip()
    destination = os.getenv("TRIP_DESTINATION", "").strip()
    trip_date = os.getenv("TRIP_DATE", "").strip()
    trip_time = os.getenv("TRIP_TIME", "").strip()

    if not all([origin, destination, trip_date, trip_time]):
        print("[ERRO CONFIG] Faltam dados obrigatórios da viagem (TRIP_ORIGIN, TRIP_DESTINATION, TRIP_DATE, TRIP_TIME).")
        return

    departure_dt = datetime.strptime(f"{trip_date} {trip_time}", "%Y-%m-%d %H:%M").replace(tzinfo=LISBON_TZ)
    now = datetime.now(LISBON_TZ)

    window_pre_start = departure_dt - timedelta(hours=24, minutes=10)
    window_open_exact = departure_dt - timedelta(hours=24) + timedelta(seconds=1)
    window_end = departure_dt - timedelta(hours=21)

    print(f"[TEMPO] Agora: {now.strftime('%Y-%m-%d %H:%M:%S')} | Partida: {departure_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    if now < window_pre_start:
        print(f"[AGUARDAR] Fora da janela ativa. O agente iniciará às {window_pre_start.strftime('%Y-%m-%d %H:%M')}.")
        return

    if now > window_end:
        print("[EXPIRADO] Passaram 2 horas desde a abertura sem conseguir emitir bilhete.")
        update_github_variable("BOOKING_STATUS", "FAILED_EXPIRED")
        send_email(
            subject="Não consegui comprar bilhete",
            body=(
                f"Não consegui comprar bilhete.\n\n"
                f"Detalhes da viagem procurada:\n"
                f"- Trajeto: {origin} -> {destination}\n"
                f"- Data/Hora: {trip_date} às {trip_time} (Intercidades, 2ª Classe)\n"
                f"- Janela de tentativas concluída sem vagas disponíveis."
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
        print(f"[SUCESSO] Bilhete emitido! {details}")
        update_github_variable("BOOKING_STATUS", "BOUGHT")
        send_email(
            subject="Bilhete OK",
            body=(
                f"Bilhete OK\n\n"
                f"Detalhes da reserva emitida (0,00 € - Passe Ferroviário Verde):\n"
                f"- Trajeto: {origin} -> {destination}\n"
                f"- Data/Hora: {trip_date} às {trip_time}\n"
                f"- Alocação: {details}\n"
            ),
            pdf_bytes=pdf_bytes,
            pdf_filename=f"Bilhete_CP_{trip_date}_{trip_time.replace(':', 'h')}.pdf",
        )
    else:
        print(f"[TENTATIVA SEM SUCESSO] {details}. Nova tentativa no próximo ciclo de 5 minutos.")


if __name__ == "__main__":
    main()
