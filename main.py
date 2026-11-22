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
    """Atualiza uma Repository Variable via GitHub REST API."""
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
    """Envia e-mail via SMTP (Gmail) com suporte opcional para anexar o PDF do bilhete."""
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
    """Fecha o banner de cookies OneTrust caso esteja visível."""
    try:
        cookie_btn = page.locator("#onetrust-reject-all-handler, #onetrust-accept-btn-handler").first
        if cookie_btn.is_visible(timeout=4000):
            cookie_btn.click()
            page.wait_for_timeout(500)
    except Exception:
        pass


def fill_station_input(page, input_locator, station_name: str) -> None:
    """Preenche estação de origem/destino e seleciona a sugestão do autocomplete."""
    input_locator.click()
    input_locator.fill("")
    input_locator.type(station_name, delay=60)
    page.wait_for_timeout(1200)
    # Tenta clicar na primeira opção da lista suspensa de estações
    suggestion = page.locator(".ui-autocomplete li, [role='listbox'] [role='option'], .autocomplete-suggestion").first
    if suggestion.is_visible(timeout=3000):
        suggestion.click()
    else:
        input_locator.press("Enter")


def select_coach_and_seat(page) -> str:
    """
    Seleciona a última carruagem disponível (prioridade: 24, depois 23, ou última da lista)
    e tenta escolher o lugar 68 ou 63. Caso indisponíveis, mantém o lugar aleatório.
    """
    info_lugar = "Lugar aleatório atribuído pelo sistema"
    try:
        # Procura seletor/abas de carruagens no mapa de lugares
        coach_elements = page.locator("[data-coach], .coach-selector button, .carruagem-item, select[name*='coach'] option")
        count = coach_elements.count()

        if count > 0:
            selected_coach = None
            # Prioridade explícita: Carruagem 24 -> Carruagem 23 -> Última disponível
            for target_coach in ["24", "23"]:
                candidate = page.locator(f"[data-coach='{target_coach}'], button:has-text('{target_coach}'), .carruagem-item:has-text('{target_coach}')").first
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

            # Dentro da carruagem selecionada, tenta lugar 68 e depois 63
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
    """
    Circuit Breaker obrigatório: confirma que o valor total a pagar é exatamente 0,00€.
    """
    page_text = page.locator("body").inner_text()
    # Procura padrões de total monetário junto a palavras-chave de resumo/pagamento
    matches = re.findall(r"(?:Total|A pagar|Valor total)[^\d€]{0,25}(\d+[.,]\d{2})\s*€", page_text, re.IGNORECASE)
    if not matches:
        # Fallback: procura qualquer valor em euros destacado no resumo final
        matches = re.findall(r"(\d+[.,]\d{2})\s*€", page_text)

    if not matches:
        print("[SEGURANÇA] Não foi possível confirmar o valor total no ecrã. Abortar por precaução.")
        return False

    # O último valor total apresentado no checkout tem de ser estritamente 0.00
    final_amount = matches[-1].replace(",", ".")
    print(f"[SEGURANÇA] Valor total detetado: {final_amount} €")
    return float(final_amount) == 0.0


def attempt_ticket_purchase(
    origin: str,
    destination: str,
    trip_date: str,
    trip_time: str,
    wait_until_dt: datetime | None = None,
) -> tuple[bool, str, bytes | None]:
    """
    Executa uma tentativa completa de pesquisa e compra do bilhete.
    Retorna: (sucesso: bool, detalhe_mensagem: str, pdf_bytes: bytes | None)
    """
    pass_code = os.environ["CP_PASS_CODE"]
    passenger_name = os.environ["PASSENGER_NAME"]
    passenger_id = os.environ["PASSENGER_ID_NUMBER"]
    passenger_email = os.environ["PASSENGER_EMAIL"]

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
            print(f"[NAVEGAÇÃO] A aceder a https://cp.pt/pt/ para {origin} -> {destination} ({trip_date} {trip_time})")
            page.goto("https://cp.pt/pt/", wait_until="domcontentloaded", timeout=30000)
            handle_cookies(page)

            # 1. Preencher formulário de pesquisa
            origin_input = page.locator("input[placeholder*='Origem'], input[name*='origem'], #origem").first
            dest_input = page.locator("input[placeholder*='Destino'], input[name*='destino'], #destino").first
            fill_station_input(page, origin_input, origin)
            fill_station_input(page, dest_input, destination)

            # Preencher Data (YYYY-MM-DD ou DD-MM-YYYY conforme o campo)
            date_input = page.locator("input[name*='data'], input[placeholder*='Data'], #datepicker-first").first
            date_input.click()
            date_input.fill(trip_date)
            date_input.press("Escape")

            # Garantir 2ª Classe / Turística
            class_dropdown = page.locator("text=2ª Classe").first
            if not class_dropdown.is_visible():
                selector = page.locator("button:has-text('Classe'), select[name*='classe']").first
                if selector.is_visible():
                    selector.click()
                    page.locator("text=2ª Classe").first.click()

            # Sincronização exata às T - 24h00m01s se estivermos na janela de pré-abertura
            if wait_until_dt:
                now = datetime.now(LISBON_TZ)
                seconds_to_wait = (wait_until_dt - now).total_seconds()
                if 0 < seconds_to_wait <= 660:
                    print(f"[SYNC] A aguardar {seconds_to_wait:.2f}s até à abertura exata da janela ({wait_until_dt.strftime('%H:%M:%S')})...")
                    time.sleep(seconds_to_wait)

            # Submeter Pesquisa
            search_btn = page.locator("button:has-text('Pesquisar'), input[type='submit'][value*='Pesquisar']").first
            search_btn.click()
            page.wait_for_load_state("networkidle", timeout=20000)

            # 2. Localizar o comboio Intercidades na hora exata
            train_row = page.locator(f"tr:has-text('{trip_time}'):has-text('Intercidades'), .train-row:has-text('{trip_time}'):has-text('IC')").first
            if train_row.count() == 0:
                return False, f"Comboio Intercidades das {trip_time} não encontrado nos resultados.", None

            row_text = train_row.inner_text()
            if "Lotado" in row_text or "Esgotado" in row_text or "Indisponível" in row_text:
                return False, f"Comboio das {trip_time} encontra-se lotado.", None

            # Selecionar o comboio e avançar
            select_btn = train_row.locator("button, a, input[type='radio']").first
            select_btn.click()
            page.wait_for_timeout(1000)

            continue_btn = page.locator("button:has-text('Continuar'), button:has-text('Avançar'), button:has-text('Comprar')").first
            continue_btn.click()
            page.wait_for_timeout(2000)

            # Verificar mensagem explícita de bloqueio/indisponibilidade
            body_text = page.locator("body").inner_text()
            if "Não foi possível iniciar o processo de compra" in body_text:
                return False, "Mensagem detetada: 'Não foi possível iniciar o processo de compra'.", None

            # 3. Preencher dados do passageiro e Passe Ferroviário Verde
            name_field = page.locator("input[name*='nome'], input[placeholder*='Nome']").first
            if name_field.is_visible(timeout=5000):
                name_field.fill(passenger_name)

            id_field = page.locator("input[name*='documento'], input[name*='identificacao'], input[placeholder*='Identificação'], input[placeholder*='Cartão']").first
            if id_field.is_visible():
                id_field.fill(passenger_id)

            email_field = page.locator("input[type='email'], input[name*='email']").first
            if email_field.is_visible():
                email_field.fill(passenger_email)

            # Selecionar Desconto: Passe Ferroviário Verde
            discount_select = page.locator("select[name*='desconto'], select[id*='desconto']").first
            if discount_select.is_visible():
                discount_select.select_option(label="Passe Ferroviário Verde")
            else:
                # Caso seja dropdown customizado
                page.locator("text=Desconto").first.click()
                page.locator("text=Passe Ferroviário Verde").first.click()

            page.wait_for_timeout(1000)

            # Preencher "Informação adicional" com o código do passe
            extra_info_field = page.locator("input[name*='adicional'], input[placeholder*='Informação adicional'], input[name*='cartao'], input[name*='passe']").first
            extra_info_field.fill(pass_code)

            page.locator("button:has-text('Continuar'), button:has-text('Avançar')").first.click()
            page.wait_for_timeout(2000)

            # Verificar novamente se ocorreu erro ao validar o passe/lotação
            if "Não foi possível iniciar o processo de compra" in page.locator("body").inner_text():
                return False, "Erro ao validar Passe Ferroviário Verde / sem lugares disponíveis para o passe.", None

            # 4. Escolha de Carruagem (24/23) e Lugar (68/63)
            seat_summary = select_coach_and_seat(page)
            next_after_seat = page.locator("button:has-text('Continuar'), button:has-text('Avançar'), button:has-text('Confirmar')").first
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

            # 7. Tentar descarregar o PDF do bilhete na página de confirmação
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
    # Se o utilizador acionou manualmente com reset_status=true, reinicia para PENDING
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

    # Calcular janela temporal (Hora de Lisboa)
    departure_dt = datetime.strptime(f"{trip_date} {trip_time}", "%Y-%m-%d %H:%M").replace(tzinfo=LISBON_TZ)
    now = datetime.now(LISBON_TZ)

    window_pre_start = departure_dt - timedelta(hours=24, minutes=10)
    window_open_exact = departure_dt - timedelta(hours=24) + timedelta(seconds=1)
    window_end = departure_dt - timedelta(hours=22)

    print(f"[TEMPO] Agora: {now.strftime('%Y-%m-%d %H:%M:%S')} | Partida: {departure_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. Ainda não chegou à janela de T - 24h10m
    if now < window_pre_start:
        print(f"[AGUARDAR] Fora da janela ativa. O agente iniciará às {window_pre_start.strftime('%Y-%m-%d %H:%M')}.")
        return

    # 2. Já passaram as 2 horas de tentativas (T - 22h00m) sem sucesso
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

    # 3. Dentro da janela ativa (entre T-24h10m e T-22h00m)
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
