
import subprocess
print("Iniciando Mootube...")
subprocess.run(
    "pip install --break-system-packages -r requirements.txt",
    shell=True
)

import json
import os
import re
import time
import requests
import feedparser
from pathlib import Path
from functools import wraps
from flask import (
    Flask, render_template, send_from_directory, request,
    abort, Response, jsonify
)

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

PASTA_VIDEOS = "videos"
ALLOWED_EXTENSIONS = {'.mp4', '.webm', '.mkv', '.avi', '.mov'}
MAX_SEARCH_LENGTH = 200
DOWNLOAD_TIMEOUT = 300
RATE_LIMIT = 15
FEED_CACHE_TTL = 900
INSCRITOS_FILE = "inscritos.txt"

rate_limit_store = {}
notificacao_ativa = None
notificacao_global = None
_feed_cache = {"ts": 0, "dados": []}


# ------------------------------------------------------------------
# Inscritos
# ------------------------------------------------------------------
def ler_inscritos():
    """Retorna lista de (channel_id, nome)."""
    try:
        with open(INSCRITOS_FILE) as f:
            linhas = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []
    out = []
    for l in linhas:
        partes = l.split("|", 1)
        cid = partes[0].strip()
        nome = partes[1].strip() if len(partes) > 1 else cid
        if cid:
            out.append((cid, nome))
    return out


def ja_inscrito(channel_id):
    return any(cid == channel_id for cid, _ in ler_inscritos())


def adicionar_inscrito(channel_id, nome):
    if not channel_id or ja_inscrito(channel_id):
        return False
    with open(INSCRITOS_FILE, "a") as f:
        f.write(f"{channel_id}|{nome}\n")
    print(f"[inscritos] + {nome} ({channel_id})")
    return True


def remover_inscrito(channel_id):
    itens = ler_inscritos()
    itens = [(c, n) for c, n in itens if c != channel_id]
    with open(INSCRITOS_FILE, "w") as f:
        for c, n in itens:
            f.write(f"{c}|{n}\n")
    print(f"[inscritos] - {channel_id}")


def registrar_canal_do_video(url_ou_id):
    """Pega channel_id do vídeo e adiciona em inscritos.txt."""
    try:
        r = subprocess.run(
            ["yt-dlp", url_ou_id, "--dump-json",
             "--no-warnings", "--no-playlist"],
            capture_output=True, text=True, timeout=60
        )
        linha = r.stdout.strip().splitlines()
        if not linha:
            return
        info = json.loads(linha[0])
        cid = info.get("channel_id")
        nome = info.get("uploader") or info.get("channel") or cid
        if cid:
            adicionar_inscrito(cid, nome)
    except Exception as e:
        print(f"[inscritos] erro ao registrar: {e}")


# ------------------------------------------------------------------
# Feed via RSS dos canais inscritos
# ------------------------------------------------------------------
def buscar_feed(quantidade=60):
    videos = []
    for cid, nome in ler_inscritos():
        try:
            url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
            f = feedparser.parse(url)
            canal_nome = f.feed.get("title", nome) or nome
            for e in f.entries:
                vid = e.get("yt_videoid")
                if not vid:
                    continue
                publicado = 0
                if e.get("published_parsed"):
                    try:
                        publicado = time.mktime(e.published_parsed)
                    except Exception:
                        publicado = 0
                videos.append({
                    "id": vid,
                    "titulo": e.get("title", ""),
                    "canal": canal_nome,
                    "channel_id": cid,
                    "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                    "publicado": publicado,
                    "published_text": e.get("published", ""),
                })
        except Exception as ex:
            print(f"[feed] erro canal {cid}: {ex}")
    videos.sort(key=lambda v: v["publicado"], reverse=True)
    return videos[:quantidade]


def buscar_feed_cached():
    agora = time.time()
    if agora - _feed_cache["ts"] > FEED_CACHE_TTL or not _feed_cache["dados"]:
        _feed_cache["dados"] = buscar_feed(60)
        _feed_cache["ts"] = agora
    return _feed_cache["dados"]


def invalidar_cache_feed():
    _feed_cache["dados"] = []
    _feed_cache["ts"] = 0


# ------------------------------------------------------------------
# Helpers gerais
# ------------------------------------------------------------------
def _parse_views(s):
    if not s:
        return 0
    limpo = re.sub(r'[^\d]', '', s)
    try:
        return int(limpo) if limpo else 0
    except ValueError:
        return 0


def notificar(mensagem):
    global notificacao_ativa
    notificacao_ativa = mensagem


def verificar_notificacao_global():
    global notificacao_global
    try:
        response = requests.get(
            'https://raw.githubusercontent.com/themooproject/mootube/main/notification.txt',
            timeout=5
        )
        if response.status_code == 200:
            mensagem = response.text.strip()
            notificacao_global = mensagem if mensagem else None
        else:
            notificacao_global = None
    except Exception:
        notificacao_global = None


def rate_limit(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = request.remote_addr
        now = time.time()
        if client_ip not in rate_limit_store:
            rate_limit_store[client_ip] = []
        rate_limit_store[client_ip] = [
            t for t in rate_limit_store[client_ip] if now - t < 60
        ]
        if len(rate_limit_store[client_ip]) >= RATE_LIMIT:
            return "Limite de downloads excedido. Aguarde um momento.", 429
        rate_limit_store[client_ip].append(now)
        return f(*args, **kwargs)
    return decorated_function


def sanitize_filename(filename):
    filename = re.sub(r'[^\w\s\-\.]', '', filename)
    filename = re.sub(r'\s+', ' ', filename)
    filename = filename[:200]
    return filename.strip()


def validate_video_path(filename):
    safe_path = os.path.normpath(os.path.join(PASTA_VIDEOS, filename))
    if not safe_path.startswith(os.path.normpath(PASTA_VIDEOS)):
        return False, None
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, None
    return True, safe_path


def is_url(input_text):
    url_pattern = re.compile(
        r'^https?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return bool(url_pattern.match(input_text))


os.makedirs(PASTA_VIDEOS, exist_ok=True)


def ler_urls_do_arquivo():
    try:
        with open('urls.txt', 'r') as f:
            return [linha.strip() for linha in f if linha.strip()]
    except Exception:
        return []


todos_videos = []


def atualizar_lista_videos():
    global todos_videos
    todos_videos = []
    for arquivo in os.listdir(PASTA_VIDEOS):
        if arquivo.endswith(tuple(ALLOWED_EXTENSIONS)):
            is_safe, _ = validate_video_path(arquivo)
            if is_safe:
                todos_videos.append(arquivo)


atualizar_lista_videos()


def verificar_atualizacao():
    try:
        local_version = "0.0.0"
        if os.path.exists('version.txt'):
            with open('version.txt', 'r') as f:
                local_version = f.read().strip()
        response = requests.get(
            'https://raw.githubusercontent.com/themooproject/mootube/main/version.txt',
            timeout=5
        )
        if response.status_code == 200:
            remote_version = response.text.strip()
            if remote_version != local_version:
                return True, remote_version
    except Exception:
        pass
    return False, None


# ------------------------------------------------------------------
# Rotas
# ------------------------------------------------------------------
@app.route('/')
def index():
    global notificacao_ativa
    urls = ler_urls_do_arquivo()
    tem_atualizacao, versao = verificar_atualizacao()
    verificar_notificacao_global()

    return render_template(
        'index.html',
        videos=todos_videos,
        urls=urls,
        tem_atualizacao=tem_atualizacao,
        versao=versao,
        notificacao=notificacao_ativa,
        notificacao_global=notificacao_global
    )


@app.route('/feed-api')
def feed_api():
    return jsonify(buscar_feed_cached())


@app.route('/feed-canais')
def feed_canais():
    return jsonify([
        {"channel_id": cid, "nome": nome}
        for cid, nome in ler_inscritos()
    ])


@app.route('/feed-remover', methods=['POST'])
def feed_remover():
    cid = request.form.get("channel_id", "").strip()
    if not cid:
        return jsonify({"ok": False, "erro": "channel_id vazio"}), 400
    remover_inscrito(cid)
    invalidar_cache_feed()
    return jsonify({"ok": True})


@app.route('/feed-mais')
@rate_limit
def feed_mais():
    try:
        invalidar_cache_feed()
        return jsonify(buscar_feed(60))
    except Exception as e:
        print(f"[feed-mais] erro: {e}")
        return jsonify([])


@app.route('/stream')
def stream():
    def generate():
        ultima_mensagem = None
        while True:
            verificar_notificacao_global()
            if notificacao_global != ultima_mensagem:
                ultima_mensagem = notificacao_global
                yield f"data: {notificacao_global or ''}\n\n"
            time.sleep(5)
    return Response(generate(), mimetype="text/event-stream")


@app.route('/video/<path:nome_video>')
def serve_video(nome_video):
    nome_video_safe = sanitize_filename(nome_video)
    is_safe, _ = validate_video_path(nome_video_safe)
    if not is_safe:
        abort(404)
    response = send_from_directory(PASTA_VIDEOS, nome_video_safe)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route('/url', methods=['POST'])
@rate_limit
def baixar_urls():
    urls = ler_urls_do_arquivo()
    if not urls:
        return "Nenhuma URL encontrada no arquivo urls.txt.", 400

    videos_path = Path(PASTA_VIDEOS).resolve()
    sucessos = 0
    erros = 0

    for url in urls:
        try:
            registrar_canal_do_video(url)
            cmd = [
                "yt-dlp", url,
                "-o", str(videos_path / "%(title)s.%(ext)s"),
                "--no-warnings", "--restrict-filenames",
                "--no-playlist", "--socket-timeout", "30"
            ]
            subprocess.run(cmd, check=True, capture_output=True,
                           text=True, timeout=DOWNLOAD_TIMEOUT)
            sucessos += 1
        except Exception as e:
            erros += 1
            print(f"Erro ao baixar {url}: {e}")

    atualizar_lista_videos()
    invalidar_cache_feed()

    if erros == 0:
        return render_template('sucess.html')
    elif sucessos > 0:
        return f"Baixados {sucessos} videos, {erros} falhas."
    else:
        return render_template('error.html')


@app.route('/pesquisar')
@rate_limit
def pesquisar():
    termo = request.args.get("busca", "").strip()

    if not termo:
        return "Nenhum termo de busca fornecido.", 400
    if len(termo) > MAX_SEARCH_LENGTH:
        return "Termo de busca muito longo.", 400

    if is_url(termo):
        videos_path = Path(PASTA_VIDEOS).resolve()
        try:
            registrar_canal_do_video(termo)
            cmd = [
                "yt-dlp", termo,
                "-o", str(videos_path / "%(title)s.%(ext)s"),
                "--no-warnings", "--restrict-filenames",
                "--no-playlist", "--socket-timeout", "30"
            ]
            subprocess.run(cmd, check=True, capture_output=True,
                           text=True, timeout=DOWNLOAD_TIMEOUT)
            atualizar_lista_videos()
            invalidar_cache_feed()
            return render_template('sucess.html')
        except Exception as e:
            print(f"Erro: {e}")
            return render_template('error.html')

    # não é URL: mostra resultados com botão Baixar
    try:
        resultados = buscar_resultados(termo, quantidade=12)
    except Exception as e:
        print(f"[pesquisar] erro: {e}")
        resultados = []

    return render_template('search.html',
                           termo=termo,
                           resultados=resultados)


def buscar_resultados(termo, quantidade=12):
    """Busca via yt-dlp (ytsearch) — sem key, sem tubescrape."""
    termo = re.sub(r'[;&|`$<>]', '', termo).strip()[:MAX_SEARCH_LENGTH]
    if not termo:
        return []
    try:
        r = subprocess.run(
            ["yt-dlp", f"ytsearch{quantidade}:{termo}",
             "--dump-json", "--flat-playlist",
             "--no-warnings", "--socket-timeout", "20"],
            capture_output=True, text=True, timeout=60
        )
    except Exception as e:
        print(f"[search] erro: {e}")
        return []

    out = []
    for linha in r.stdout.strip().splitlines():
        try:
            info = json.loads(linha)
        except json.JSONDecodeError:
            continue
        vid = info.get("id")
        if not vid:
            continue
        out.append({
            "id": vid,
            "titulo": info.get("title", ""),
            "canal": info.get("uploader") or info.get("channel") or "",
            "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
    return out


@app.route('/baixar_feed', methods=['POST'])
@rate_limit
def baixar_feed():
    video_id = request.form.get("video_id", "").strip()
    if not re.match(r'^[A-Za-z0-9_-]{6,20}$', video_id):
        return jsonify({"ok": False, "erro": "ID inválido"}), 400

    url = f"https://www.youtube.com/watch?v={video_id}"
    videos_path = Path(PASTA_VIDEOS).resolve()

    registrar_canal_do_video(url)

    cmd = [
        "yt-dlp", url,
        "-o", str(videos_path / "%(title)s.%(ext)s"),
        "--no-warnings", "--restrict-filenames",
        "--no-playlist", "--socket-timeout", "30"
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True,
                       text=True, timeout=DOWNLOAD_TIMEOUT)
        atualizar_lista_videos()
        invalidar_cache_feed()
        return jsonify({"ok": True})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "erro": "Timeout"}), 504
    except subprocess.CalledProcessError as e:
        print(f"[baixar_feed] erro: {e.stderr}")
        return jsonify({"ok": False, "erro": "Falha no download"}), 500
    except FileNotFoundError:
        print("[baixar_feed] yt-dlp não encontrado no PATH")
        return jsonify({"ok": False, "erro": "yt-dlp não instalado"}), 500


@app.route('/api/videos')
def api_videos():
    atualizar_lista_videos()
    return jsonify(todos_videos)


@app.route('/filtrar')
def filtrar():
    termo = request.args.get("busca", "").strip().lower()
    if not termo:
        return jsonify(todos_videos)
    return jsonify([v for v in todos_videos if termo in v.lower()])


@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@app.route("/atualizar")
def atualizar():
    try:
        subprocess.run(
            ["pip", "install", "-U", "flask", "yt-dlp", "feedparser", "--break-system-packages"],
            check=True, capture_output=True
        )
        subprocess.run(
            "git clone https://github.com/mootube-project/mootube && "
            "cp -r mootube/* . && rm -rf mootube",
            shell=True, check=True, capture_output=True
        )
        return render_template('update-sucess.html')
    except subprocess.CalledProcessError as e:
        print(f"Erro: {e.stderr}")
        return render_template('update-error.html')


@app.route('/deletar/<nome_video>', methods=['GET'])
def deletar_video(nome_video):
    try:
        caminho = os.path.join(PASTA_VIDEOS, nome_video)
        if os.path.exists(caminho):
            os.remove(caminho)
        atualizar_lista_videos()
        return render_template('delete-sucess.html')
    except Exception:
        return render_template('delete-error.html')


@app.route("/features")
def features():
    versao = "0.0.0"
    try:
        if os.path.exists("version.txt"):
            with open("version.txt", "r") as f:
                versao = f.read().strip()
    except Exception:
        pass
    return render_template("features.html", versao=versao)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, use_reloader=True, debug=False)