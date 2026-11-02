#!/usr/bin/env python3
"""
Baixa cotações de commodities/cripto NO SERVIDOR do GitHub (sem CORS) e grava data/commodities.json.

Os contratos futuros abaixo são os mesmos que o Investing.com mostra como
"Brent Oil", "Crude Oil WTI" e "Gold" (front-month), então os números batem.
O Investing não tem API pública e bloqueia scraping; por isso a fonte é o endpoint
de gráfico do Yahoo Finance (gratuito, sem chave).

Regra de segurança: se um ativo falhar, mantém o valor anterior do arquivo.
"""
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


def baixar(simbolo):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}?range=5d&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


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


def main():
    antigo = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            antigo = json.load(f)
    except (OSError, ValueError):
        pass

    saida = dict(antigo.get("data") or {})
    ok = 0
    for i, (chave, simbolo) in enumerate(ATIVOS.items()):
        if i:
            time.sleep(1)
        try:
            bloco = calcular(baixar(simbolo))
            bloco["symbol"] = simbolo
            saida[chave] = bloco
            ok += 1
            print(f"OK    {chave} ({simbolo}) {bloco['c']} {bloco['dp']:+.2f}%")
        except Exception as e:  # noqa: BLE001
            print(f"FALHA {chave} ({simbolo}): {type(e).__name__}: {str(e)[:120]} (mantendo anterior)")

    if not ok:
        print("Nenhum ativo atualizado.")
        return 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "source": "Yahoo Finance (futuros, equivalentes ao Investing.com)",
                   "data": saida}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print("commodities.json atualizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
