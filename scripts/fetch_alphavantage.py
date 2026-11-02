#!/usr/bin/env python3
"""
Baixa séries macro dos EUA da Alpha Vantage e grava data/macro.json.

Roda no GitHub Actions (workflow macro.yml). A chave vem do GitHub Secret
ALPHAVANTAGE_KEY (variável de ambiente) — nunca fica no código nem no navegador.

Séries (6 chamadas por execução; plano gratuito = limite diário baixo):
  CPI (mensal)  → inflação anual (YoY) calculada
  UNEMPLOYMENT  → taxa de desemprego
  NONFARM_PAYROLL → média mensal de empregos nos últimos 12 meses (série sem ajuste sazonal)
  REAL_GDP (trimestral) → crescimento ano contra ano (série sem ajuste sazonal)
  TREASURY_YIELD 10y e 2y (diário) → rendimento, histórico p/ sparkline e spread 10y−2y

Regra de segurança: se uma série falhar (limite de uso, erro de rede), o bloco
anterior do macro.json é mantido. Se nada mudou, o arquivo não é reescrito.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://www.alphavantage.co/query"
OUT = os.path.join("data", "macro.json")
PAUSA_S = float(os.environ.get("AV_PAUSA", "13"))  # respeita o limite por minuto do plano gratuito


def chamar(funcao, **params):
    q = urllib.parse.urlencode(
        {"function": funcao, "datatype": "json", "apikey": os.environ["ALPHAVANTAGE_KEY"], **params}
    )
    req = urllib.request.Request(API + "?" + q, headers={"User-Agent": "fx-terminal-macro/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read().decode("utf-8"))
    if not isinstance(j, dict) or "data" not in j:
        msg = (j.get("Information") or j.get("Note") or j.get("Error Message") or "resposta inesperada") if isinstance(j, dict) else "resposta inesperada"
        raise RuntimeError(str(msg)[:160])
    return j


def pontos(j):
    """[(data, valor)] válidos, do mais recente para o mais antigo (ignora '.')."""
    out = []
    for d in j.get("data", []):
        try:
            out.append((d["date"], float(d["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def r(v, n=2):
    return None if v is None else round(v, n)


def ano_antes(date):
    y, mo, d = date.split("-")
    return f"{int(y) - 1}-{mo}-{d}"


def comparar_ano(p, f, casas):
    """Compara cada ponto com o MESMO período do ano anterior (neutraliza a sazonalidade).
    Retorna (atual, anterior, data, data_anterior) usando os dois pontos mais recentes calculáveis."""
    m = dict(p)

    def val(date):
        ant = ano_antes(date)
        if date in m and m.get(ant):
            return round(f(m[date], m[ant]), casas)
        return None

    datas = [d for d, _ in p]
    for i, d in enumerate(datas[:6]):
        a = val(d)
        if a is None:
            continue
        for d2 in datas[i + 1:i + 4]:
            b = val(d2)
            if b is not None:
                return a, b, d, d2
        return a, None, d, None
    return None


def calc_cpi(p):
    r_ = comparar_ano(p, lambda c, a: (c / a - 1) * 100, 2)
    if not r_:
        return None
    return {"yoy": r_[0], "prev_yoy": r_[1], "date": r_[2], "prev_date": r_[3]}


def calc_unemployment(p):
    if len(p) < 2:
        return None
    if not 0 < p[0][1] < 30:
        return None
    return {"value": r(p[0][1]), "prev": r(p[1][1]), "date": p[0][0]}


def calc_payroll(p):
    # A série da Alpha Vantage NÃO é ajustada sazonalmente (a variação mensal não bate com o
    # número oficial do BLS). Usamos a média mensal dos últimos 12 meses (em milhares), que
    # elimina a sazonalidade.
    r_ = comparar_ano(p, lambda c, a: (c - a) / 12, 0)
    if not r_:
        return None
    return {"avg12_k": int(r_[0]), "prev_avg12_k": None if r_[1] is None else int(r_[1]),
            "date": r_[2], "prev_date": r_[3]}


def calc_gdp(p):
    # Série trimestral também sem ajuste sazonal → crescimento ano contra ano (a/a).
    r_ = comparar_ano(p, lambda c, a: (c / a - 1) * 100, 2)
    if not r_:
        return None
    return {"yoy": r_[0], "prev_yoy": r_[1], "date": r_[2], "prev_date": r_[3]}


def calc_ust(p):
    if len(p) < 2 or not 0 < p[0][1] < 25:
        return None
    return {
        "value": r(p[0][1], 3),
        "prev": r(p[1][1], 3),
        "date": p[0][0],
        "prev_date": p[1][0],
        "history": [r(v, 3) for _, v in reversed(p[:30])],  # antigo → recente
    }


TAREFAS = [
    ("cpi", "CPI", {"interval": "monthly"}, calc_cpi),
    ("unemployment", "UNEMPLOYMENT", {}, calc_unemployment),
    ("payroll", "NONFARM_PAYROLL", {}, calc_payroll),
    ("gdp", "REAL_GDP", {"interval": "quarterly"}, calc_gdp),
    ("us10y", "TREASURY_YIELD", {"interval": "daily", "maturity": "10year"}, calc_ust),
    ("us2y", "TREASURY_YIELD", {"interval": "daily", "maturity": "2year"}, calc_ust),
]


def montar_saida(antigo, novos):
    out = {k: v for k, v in antigo.items() if k not in ("updated", "source")}
    tre = dict(out.get("treasury") or {})
    for chave, bloco in novos.items():
        if chave in ("us10y", "us2y"):
            tre[chave] = bloco
        else:
            out[chave] = bloco
    a, b = tre.get("us10y"), tre.get("us2y")
    if a and b and a.get("date") == b.get("date"):
        tre["spread_10y_2y"] = round(a["value"] - b["value"], 3)
        if a.get("prev_date") == b.get("prev_date"):
            tre["spread_prev"] = round(a["prev"] - b["prev"], 3)
    if tre:
        out["treasury"] = tre
    return out


def main():
    if not os.environ.get("ALPHAVANTAGE_KEY"):
        print("ERRO: secret ALPHAVANTAGE_KEY não definido.")
        return 1

    antigo = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            antigo = json.load(f)
    except (OSError, ValueError):
        pass

    novos = {}
    for i, (chave, funcao, params, calc) in enumerate(TAREFAS):
        if i:
            time.sleep(PAUSA_S)
        try:
            bloco = calc(pontos(chamar(funcao, **params)))
            if bloco is None:
                raise RuntimeError("dados insuficientes/fora do intervalo esperado")
            novos[chave] = bloco
            print(f"OK   {chave}")
        except (urllib.error.URLError, RuntimeError, ValueError, OSError) as e:
            print(f"FALHA {chave}: {type(e).__name__}: {str(e)[:160]} (mantendo valor anterior)")

    if not novos:
        print("Nenhuma série atualizada.")
        return 1

    saida = montar_saida(antigo, novos)
    sem_ts = lambda d: {k: v for k, v in d.items() if k not in ("updated", "source")}
    if sem_ts(saida) == sem_ts(antigo):
        print("Sem mudanças; arquivo mantido.")
        return 0

    saida["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    saida["source"] = "Alpha Vantage"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print("macro.json atualizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
