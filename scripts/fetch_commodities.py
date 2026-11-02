#!/usr/bin/env python3
"""
Baixa cotações de commodities/cripto NO SERVIDOR do GitHub (sem CORS) e grava data/commodities.json.

Os contratos futuros abaixo são os mesmos que o Investing.com mostra como
"Brent Oil", "Crude Oil WTI" e "Gold" (front-month), então os números batem.
O Investing não tem API pública e bloqueia scraping; por isso a fonte é o endpoint
de gráfico do Yahoo Finance (gratuito, sem chave).

Fontes, em ordem, para cada ativo (a primeira que responder vale):
  1) Yahoo Finance (query1, depois query2)  — o GitHub às vezes recebe HTTP 429 (IP compartilhado)
  2) Stooq (petróleo/ouro, CSV sem chave)
  3) Bitcoin → Coinbase · Ouro → PAXG-USD na Coinbase (token lastreado em 1 oz de ouro, ~spot)
     Brent/WTI → Alpha Vantage (secret ALPHAVANTAGE_KEY; fechamento DIÁRIO, no máx. 1 consulta/6h por ativo
     para não estourar o limite gratuito de 25/dia, já usado em parte pelo workflow macro.yml)
Regra de segurança: se um ativo falhar em todas, mantém o valor anterior do arquivo.
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

OUT = os.path.join("data", "commodities.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

# chave do painel -> símbolo Yahoo
ATIVOS = {
    "wti":  "BZ=F",      # Brent (o painel usa a chave 'wti' para a linha de petróleo)
    "wtiUS": "CL=F",     # WTI
    "gold": "GC=F",      # Ouro futuro
    "btc":  "BTC-USD",   # Bitcoin
}


STOOQ = {"BZ=F": "cb.f", "CL=F": "cl.f", "GC=F": "gc.f"}   # Brent, WTI, Ouro


def http(url, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def baixar(simbolo):
    ultimo = None
    for host in ("query1", "query2"):
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{simbolo}?range=5d&interval=1d"
            return json.loads(http(url, "application/json"))
        except Exception as e:  # noqa: BLE001
            ultimo = e
            time.sleep(2)
    raise ultimo


def calcular(j):
    res = j["chart"]["result"][0]
    meta = res["meta"]
    preco = float(meta["regularMarketPrice"])
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    barras = [(t, c) for t, c in zip(ts, closes) if c is not None]
    t_mkt = meta.get("regularMarketTime")
    dia = lambda t: datetime.fromtimestamp(t, timezone.utc).date()
    if len(barras) >= 2 and t_mkt and dia(barras[-1][0]) == dia(t_mkt):
        anterior = barras[-2][1]          # barra de hoje em andamento → fechamento anterior
    elif barras:
        anterior = barras[-1][1]          # mercado fechado → último fechamento
    else:
        anterior = meta.get("chartPreviousClose") or preco
    if not preco > 0 or not anterior > 0:
        raise ValueError("preço inválido")
    return {
        "c": round(preco, 2),
        "d": round(preco - anterior, 2),
        "dp": round((preco - anterior) / anterior * 100, 2),
        "pc": round(anterior, 2),
        "asof": datetime.fromtimestamp(t_mkt, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t_mkt else None,
    }


def montar(preco, anterior, asof=None):
    if not preco > 0 or not anterior > 0:
        raise ValueError("preço inválido")
    return {
        "c": round(preco, 2),
        "d": round(preco - anterior, 2),
        "dp": round((preco - anterior) / anterior * 100, 2),
        "pc": round(anterior, 2),
        "asof": asof,
    }


def stooq(simbolo):
    """Petróleo/ouro: cotação atual (quote) + fechamento anterior (histórico diário)."""
    cod = STOOQ[simbolo]
    q = list(csv.DictReader(io.StringIO(http(f"https://stooq.com/q/l/?s={cod}&f=sd2t2ohlcv&h&e=csv"))))[0]
    preco = float(q["Close"])
    h = [r for r in csv.DictReader(io.StringIO(http(f"https://stooq.com/q/d/l/?s={cod}&i=d")))
         if r.get("Close") not in (None, "", "null")]
    if not h:
        raise ValueError("histórico vazio")
    anterior = float(h[-2]["Close"]) if len(h) > 1 and h[-1]["Date"] == q["Date"] else float(h[-1]["Close"])
    return montar(preco, anterior, f"{q['Date']}T{q['Time']}Z")


def coinbase(produto):
    """Último preço vs abertura de 24h (Coinbase Exchange, sem chave)."""
    j = json.loads(http(f"https://api.exchange.coinbase.com/products/{produto}/stats", "application/json"))
    return montar(float(j["last"]), float(j["open"]), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


ALPHA = {"BZ=F": "BRENT", "CL=F": "WTI"}
ANTIGO_POR_SIMBOLO = {}      # preenchido em main() com o conteúdo anterior do arquivo
AV_INTERVALO_S = 6 * 3600


def alphavantage(simbolo):
    """Brent/WTI: último fechamento diário vs anterior. Reaproveita o valor se consultado há <6h."""
    ant = ANTIGO_POR_SIMBOLO.get(simbolo)
    if ant and ant.get("via") == "alphavantage" and ant.get("fetched"):
        try:
            idade = time.time() - datetime.strptime(ant["fetched"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if idade < AV_INTERVALO_S:
                return dict(ant)
        except ValueError:
            pass
    chave = os.environ.get("ALPHAVANTAGE_KEY")
    if not chave:
        raise RuntimeError("secret ALPHAVANTAGE_KEY ausente")
    j = json.loads(http(f"https://www.alphavantage.co/query?function={ALPHA[simbolo]}&interval=daily&apikey={chave}", "application/json"))
    if not isinstance(j, dict) or "data" not in j:
        raise RuntimeError(str(j.get("Information") or j.get("Note") or j.get("Error Message") or "resposta inesperada")[:90])
    pts = sorted(((d["date"], float(d["value"])) for d in j["data"] if d.get("value") not in (None, "", ".")), reverse=True)
    if len(pts) < 2:
        raise ValueError("dados insuficientes")
    bloco = montar(pts[0][1], pts[1][1], pts[0][0] + "T00:00:00Z")
    bloco["fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bloco


def obter(simbolo):
    """Tenta as fontes em ordem; devolve (bloco, nome_da_fonte)."""
    tentativas = [("yahoo", lambda: calcular(baixar(simbolo)))]
    if simbolo in STOOQ:
        tentativas.append(("stooq", lambda: stooq(simbolo)))
    if simbolo == "BTC-USD":
        tentativas.append(("coinbase", lambda: coinbase("BTC-USD")))
    if simbolo == "GC=F":
        tentativas.append(("coinbase-paxg", lambda: coinbase("PAXG-USD")))
    if simbolo in ALPHA:
        tentativas.append(("alphavantage", lambda: alphavantage(simbolo)))
    erros = []
    for nome, fn in tentativas:
        try:
            return fn(), nome
        except Exception as e:  # noqa: BLE001
            erros.append(f"{nome}: {type(e).__name__} {str(e)[:60]}")
    raise RuntimeError(" | ".join(erros))


def main():
    antigo = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            antigo = json.load(f)
    except (OSError, ValueError):
        pass

    saida = dict(antigo.get("data") or {})
    for blk in saida.values():
        if isinstance(blk, dict) and blk.get("symbol"):
            ANTIGO_POR_SIMBOLO[blk["symbol"]] = blk
    ok = 0
    for i, (chave, simbolo) in enumerate(ATIVOS.items()):
        if i:
            time.sleep(1)
        try:
            bloco, fonte = obter(simbolo)
            bloco["symbol"] = simbolo
            bloco["via"] = fonte
            bloco.setdefault("fetched", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            saida[chave] = bloco
            ok += 1
            print(f"OK    {chave} ({simbolo}) via {fonte}: {bloco['c']} {bloco['dp']:+.2f}%")
        except Exception as e:  # noqa: BLE001
            print(f"FALHA {chave} ({simbolo}): {type(e).__name__}: {str(e)[:120]} (mantendo anterior)")

    if not ok:
        print("Nenhum ativo atualizado.")
        return 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "source": "Yahoo / Stooq / Coinbase / Alpha Vantage (equivalentes ao Investing.com)",
                   "data": saida}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print("commodities.json atualizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
