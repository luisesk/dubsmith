"""Verificacao pos-mux: mede o resIduo do dub no arquivo que acabou de sair.

`sync.detect` estima um delay a partir das fontes, e todo estimador erra em
alguma entrada. A garantia nao vem de estimar melhor, vem de medir o que foi
produzido. `verify_muxed` roda em cima da saida do mkvmerge ANTES dela virar o
arquivo da library, entao reprovar segura o arquivo em vez de avisar depois que
ja foi.

NOTA DE MEDICAO, verificada na marra, nao "conserte" isto:
  `sync._extract_wav(..., start_s=N)` com N > 0 emite `-ss N` antes do `-i`,
  que e seek de entrada. Ele posiciona os dois streams no mesmo instante
  absoluto do container, entao delay que mora em metadado JA esta considerado e
  o lag medido E o residuo. NAO subtraia a diferenca de `start_time` do ffprobe
  por cima: isso conta duas vezes e fabrica um residuo igual a menos o delay
  aplicado.

  Com N == 0 nao vale: o ffmpeg comeca cada stream no primeiro pacote dele e o
  offset do container some. Medido num arquivo de +958ms de metadado: janelas a
  partir de 120s leem 1 a 3ms, a mesma janela em 0 le 958ms. Por isso
  `min_start_seconds` nunca pode ser zero.

CONVENCAO DE SINAL, estabelecida injetando offsets conhecidos:
  `residual_ms` e o delay que AINDA falta aplicar no dub.
  delay_corrigido = delay_aplicado + residual_ms.
  Residuo positivo significa dub adiantado, que incomoda mais que atrasado.
"""
from __future__ import annotations

import logging
import math
import os
import statistics
import tempfile
import time
from dataclasses import dataclass, field

import numpy as np
from scipy.signal import fftconvolve

from . import probe
from .lang import lang_matches
from .sync import _extract_wav, _load

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": True,
    "windows": 7,
    # 120, nao 30: janela curta produz falso-pico. No Overlord S04E08 as janelas
    # de 30s davam lag 1707ms e 10491ms com razao ~1.05, e as MESMAS janelas em
    # 120s davam lag 0 com razao 2.5-8.5. Janela maior sobe a razao de pico, ou
    # seja aumenta a confianca em vez de afrouxar o gate. Medido em 10 arquivos
    # ja dublados: residuo identico nos dois tamanhos, nenhum veredito virou,
    # janelas uteis so subiram. Cuidado com episodio curto: em duracao menor que
    # ~15min as 7 posicoes de _FRACS colidem e o set() derruba a contagem.
    "window_seconds": 120,
    "min_start_seconds": 30,   # nunca 0: em 0 o offset de container e descartado
    "bound_seconds": 15,
    "sample_rate": 8000,
    "min_usable_windows": 3,
    "silence_dbfs": -45.0,
    "min_peak_ratio": 1.6,
    "guard_ms": 250,
    "max_early_ms": 45,        # EBU R37 permite 40, ITU BT.1359-1 detecta em 45
    "max_late_ms": 60,         # R37 novamente, o lado tolerante
    "max_spread_ms": 60,
    "consenso_ms": 25,         # distancia da mediana para a janela "concordar"
    "bimodal_min_windows": 2,  # dissidentes fortes que caracterizam degrau
    "bimodal_peak_ratio": 3.0, # pico minimo para a discordancia contar
    "min_consenso_frac": 0.66,  # fracao das janelas uteis que precisa concordar
    "auto_correct": True,
    "auto_correct_max_ms": 2000,
}

# Fracoes do tempo total, nao segundos fixos, para servir a episodio de qualquer
# duracao. A primeira nunca cai na abertura nem em 0.
_FRACS = (0.10, 0.23, 0.37, 0.50, 0.63, 0.77, 0.90)


class VerificationFailed(RuntimeError):
    """Levantada de dentro do callback do mux para o merge nunca aterrissar."""

    def __init__(self, result: "VerifyResult"):
        super().__init__(result.reason)
        self.result = result


@dataclass
class WindowResult:
    start_s: int
    lag_ms: int | None = None
    peak_ratio: float = 0.0
    ref_dbfs: float = 0.0
    dub_dbfs: float = 0.0
    ok: bool = False
    why: str = "ok"


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    residual_ms: int | None = None
    spread_ms: int | None = None
    suggested_delay_ms: int | None = None
    confident: bool = False
    ref_index: int | None = None
    ref_desc: str = ""
    n_usable: int = 0
    n_agree: int = 0
    n_windows: int = 0
    elapsed_s: float = 0.0
    windows: list = field(default_factory=list)

    def summary(self) -> str:
        head = "ok" if self.ok else f"REPROVOU({self.reason})"
        return (f"{head} residuo={self.residual_ms}ms spread={self.spread_ms}ms "
                f"concordam={self.n_agree}/{self.n_usable} de {self.n_windows} "
                f"ref[{self.ref_desc}] sugere={self.suggested_delay_ms} "
                f"em {self.elapsed_s:.1f}s")


def _cfg(cfg: dict | None, key: str):
    return (cfg or {}).get(key, DEFAULTS[key])


def _nossos_titulos(track_name: str, label_aliases: list[str] | None) -> set[str]:
    s = {(track_name or "").strip().casefold()}
    s |= {a.strip().casefold() for a in (label_aliases or []) if a.strip()}
    return {x for x in s if x}


def pick_reference(path: str, dub_lang: str, track_name: str,
                   label_aliases: list[str] | None) -> tuple[int, int, str]:
    """Devolve (indice_referencia, indice_dub, descricao).

    A referencia tem que ser faixa preexistente, nunca a nossa injecao. Dub
    correlacionado contra ele mesmo mede 0ms com pico limpo e passa: num
    arquivo so-dub isso deu lag 0, razao de pico 3.05 e aprovacao confiante e
    sem significado.

    `probe.jpn_audio_index` nao serve aqui: o ultimo recurso dele e "primeira
    faixa de audio", que num arquivo so-dub E a nossa. Sem candidato sobrando,
    o arquivo e inverificavel, e isso e quarentena, nao aprovacao.
    """
    audios = [s for s in probe.streams(path, no_cache=True)
              if s.get("codec_type") == "audio"]
    titulos = _nossos_titulos(track_name, label_aliases)

    def _nosso(s: dict) -> bool:
        tags = s.get("tags") or {}
        if not lang_matches(tags.get("language", ""), dub_lang):
            return False
        return (tags.get("title") or "").strip().casefold() in titulos

    nossos = [s for s in audios if _nosso(s)]
    if not nossos:
        raise RuntimeError(
            f"verify: nenhuma faixa {dub_lang} injetada com titulo {sorted(titulos)}")
    dub_idx = int(nossos[-1]["index"])

    outras = [s for s in audios if int(s["index"]) != dub_idx]
    if not outras:
        raise RuntimeError("verify: sem faixa de referencia preexistente "
                           "(recuso comparar o dub com ele mesmo)")
    # Titulo entra na busca alem da tag. Rip multi-idioma costuma vir sem tag
    # nenhuma: num arquivo real as faixas eram
    # "ToonFlix.in - [Hindi/Tamil/Telugu/English/Japanese]" e pegar a primeira
    # media o dub contra hindi, o que mede bem contra a referencia errada.
    jpn = [s for s in outras
           if lang_matches((s.get("tags") or {}).get("language", ""), "jpn")]
    if not jpn:
        jpn = [s for s in outras
               if any(t in ((s.get("tags") or {}).get("title") or "").casefold()
                      for t in probe._TITULO_JPN)]
    ref = (jpn or outras)[0]
    tags = ref.get("tags") or {}
    desc = (f"idx={ref['index']} lang={tags.get('language') or 'und'} "
            f"title={(tags.get('title') or '')!r}")
    if not jpn:
        desc += " SEM-JPN"
        log.warning("verify: %s nao tem faixa jpn; usando %s como referencia",
                    os.path.basename(path), desc)
    return int(ref["index"]), dub_idx, desc


def _dbfs(sig: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(sig.astype(np.float64) ** 2)))
    return 20.0 * float(np.log10(rms + 1e-12))


def _lag_e_razao(sig_a, sig_b, sr, bound_s, guard_ms):
    """Mesma correlacao do sync, mais a razao entre o pico e o melhor rival.

    A razao e o filtro de janela inutil: silencio e musica em loop produzem uma
    superficie de correlacao chata, cheia de picos quase iguais, e a razao cai
    para perto de 1. Com teto em 1.6, numa amostra de 14 arquivos, as 46
    janelas aceitas nao continham nenhum lag errado e as 24 rejeitadas
    continham todos eles.
    """
    n = max(len(sig_a), len(sig_b))
    a = np.pad(sig_a, (0, n - len(sig_a)))
    b = np.pad(sig_b, (0, n - len(sig_b)))
    corr = fftconvolve(a, b[::-1], mode="full")
    center = n - 1
    max_lag = int(bound_s * sr)
    lo = max(0, center - max_lag)
    hi = min(len(corr), center + max_lag + 1)
    bounded = corr[lo:hi]
    peak_i = int(np.argmax(bounded))
    lag = (peak_i + lo) - center
    peak = float(bounded[peak_i])
    guard = int(guard_ms / 1000.0 * sr)
    mask = np.ones(len(bounded), dtype=bool)
    mask[max(0, peak_i - guard): peak_i + guard + 1] = False
    rival = float(np.max(bounded[mask])) if mask.any() else 0.0
    return int(round(lag / sr * 1000.0)), peak / (abs(rival) + 1e-9)


def _dissidentes_fortes(uteis: list["WindowResult"], mediana: int,
                         consenso_ms: int, dist_min: int,
                         razao_forte: float) -> list["WindowResult"]:
    """Janelas que discordam da mediana e merecem credito ao discordar.

    Tres filtros, nessa ordem: fora do consenso, longe o bastante para soar
    errado se fosse a verdade daquele trecho, e com pico bem destacado.

    O terceiro filtro e o que faz a regra funcionar. Distancia sozinha nao
    separa "trecho realmente deslocado" de "janela caida em silencio ou musica
    em loop", e as duas aparecem como um lag distante. O que as separa e a
    razao entre o pico e o melhor rival: segmento de fato deslocado casa forte
    no offset proprio, janela inutil produz superficie chata. Por isso o
    arquivo bom de leitura [39, 39, 40, 40, 40, 320] nao e reprovado: a
    solitaria de 320ms nao tem pico para sustentar a discordancia.
    """
    return [w for w in uteis
            if abs(w.lag_ms - mediana) > consenso_ms
            and abs(w.lag_ms - mediana) > dist_min
            and (w.peak_ratio or 0) >= razao_forte]


def _medir_janela(path, ref_idx, dub_idx, start_s, cfg) -> WindowResult:
    sr = _cfg(cfg, "sample_rate")
    win = _cfg(cfg, "window_seconds")
    w = WindowResult(start_s=int(start_s))
    with tempfile.TemporaryDirectory(prefix="dubsmith-verify-") as td:
        a = os.path.join(td, "ref.wav")
        b = os.path.join(td, "dub.wav")
        try:
            _extract_wav(path, a, sr=sr, trim_s=win, map_idx=ref_idx,
                         start_s=int(start_s))
            _extract_wav(path, b, sr=sr, trim_s=win, map_idx=dub_idx,
                         start_s=int(start_s))
            sr_a, sig_a = _load(a)
            sr_b, sig_b = _load(b)
        except Exception as e:
            w.why = f"extract:{type(e).__name__}"
            return w
        if sr_a != sr_b or len(sig_a) < sr_a or len(sig_b) < sr_b:
            w.why = "curta"
            return w
        w.ref_dbfs = round(_dbfs(sig_a), 1)
        w.dub_dbfs = round(_dbfs(sig_b), 1)
        lag, razao = _lag_e_razao(sig_a, sig_b, sr_a,
                                  _cfg(cfg, "bound_seconds"),
                                  _cfg(cfg, "guard_ms"))
        w.lag_ms = lag
        w.peak_ratio = round(razao, 2)
        if min(w.ref_dbfs, w.dub_dbfs) < _cfg(cfg, "silence_dbfs"):
            w.why = "silencio"
        elif razao < _cfg(cfg, "min_peak_ratio"):
            w.why = "ambigua"
        else:
            w.ok = True
    return w


def verify_muxed(path: str, dub_lang: str, track_name: str,
                 label_aliases: list[str] | None,
                 applied_delay_ms: int, cfg: dict | None = None) -> VerifyResult:
    t0 = time.time()
    try:
        ref_idx, dub_idx, ref_desc = pick_reference(
            path, dub_lang, track_name, label_aliases)
    except Exception as e:
        return VerifyResult(ok=False, reason=f"sem referencia: {e}",
                            elapsed_s=round(time.time() - t0, 1))
    try:
        dur = probe.duration_seconds(path)
    except Exception as e:
        return VerifyResult(ok=False, reason=f"duracao ilegivel: {e}",
                            ref_desc=ref_desc,
                            elapsed_s=round(time.time() - t0, 1))

    win = _cfg(cfg, "window_seconds")
    lo = float(_cfg(cfg, "min_start_seconds"))
    hi = dur - win - 5.0
    n_req = int(_cfg(cfg, "windows"))
    fracs = _FRACS[:n_req] if n_req <= len(_FRACS) else _FRACS
    starts = sorted({round(max(lo, min(f * dur, hi))) for f in fracs})
    starts = [s for s in starts if lo <= s <= hi]

    res = VerifyResult(ok=False, ref_index=ref_idx, ref_desc=ref_desc,
                       n_windows=len(starts))
    if not starts:
        res.reason = f"arquivo curto demais para verificar ({dur:.0f}s)"
        res.elapsed_s = round(time.time() - t0, 1)
        return res

    res.windows = [_medir_janela(path, ref_idx, dub_idx, s, cfg) for s in starts]
    uteis = [w for w in res.windows if w.ok]
    res.n_usable = len(uteis)
    res.elapsed_s = round(time.time() - t0, 1)

    if len(uteis) < int(_cfg(cfg, "min_usable_windows")):
        res.reason = (f"inverificavel: so {len(uteis)}/{len(starts)} janelas uteis "
                      f"(silencio ou correlacao ambigua)")
        return res

    lags = sorted(w.lag_ms for w in uteis)
    res.residual_ms = int(statistics.median(lags))
    res.suggested_delay_ms = applied_delay_ms + res.residual_ms

    early = int(_cfg(cfg, "max_early_ms"))
    late = int(_cfg(cfg, "max_late_ms"))
    spread_max = int(_cfg(cfg, "max_spread_ms"))
    consenso_ms = int(_cfg(cfg, "consenso_ms"))

    # A pergunta e se a maioria das janelas concorda, nao se a pior delas
    # concorda. Espalhamento cru (max - min) e a estatistica mais fragil que
    # existe: uma janela sozinha em trecho de musica ou silencio derruba um
    # arquivo bom. Aconteceu tres vezes numa noite, com leituras como
    # [39, 39, 40, 40, 40, 320]: cinco janelas dentro de 1ms uma da outra e uma
    # solitaria 280ms fora. O arquivo estava certo.
    concordam = [l for l in lags if abs(l - res.residual_ms) <= consenso_ms]
    dissidentes = [l for l in lags if l not in concordam]
    res.n_agree = len(concordam)

    # Bimodalidade: dissidente sozinho e ruido, e a mediana existe para
    # descarta-lo. Dois ou mais dissidentes AGRUPADOS entre si sao outra coisa:
    # um segundo patamar, isto e, um trecho do episodio com offset proprio.
    # Nenhum delay unico serve, e "a maioria concorda" aprova assim mesmo.
    # Foi como 15 arquivos vazaram entre 16 e 20/08/2026, o pior deles com
    # 6848ms num terco das janelas (sid260 S01E01, 2/6). Ver tambem
    # Saint Seiya S03E01 (628ms) e One Piece S07E38 (366ms), medidos no
    # arquivo entregue e confirmados fora de sincronia por metade do episodio.
    fortes = _dissidentes_fortes(uteis, res.residual_ms, consenso_ms,
                                 dist_min=late,
                                 razao_forte=float(_cfg(cfg, "bimodal_peak_ratio")))
    bimodal = ([w.lag_ms for w in fortes]
               if len(fortes) >= int(_cfg(cfg, "bimodal_min_windows")) else None)
    minimo = max(int(_cfg(cfg, "min_usable_windows")),
                 math.ceil(len(uteis) * float(_cfg(cfg, "min_consenso_frac"))))
    # O espalhamento reportado passa a ser o das janelas que concordam, que e o
    # que descreve a qualidade do numero em que a correcao vai se apoiar.
    res.spread_ms = (max(concordam) - min(concordam)) if concordam else 0

    res.confident = (len(concordam) >= minimo and res.spread_ms <= spread_max
                     and not bimodal)

    if bimodal:
        res.reason = (f"offset em degrau: {len(bimodal)} janelas agrupadas em "
                      f"{int(statistics.median(bimodal))}ms contra mediana "
                      f"{res.residual_ms}ms; um trecho do episodio tem offset "
                      f"proprio e nenhum delay unico serve. lags={lags}")
    elif len(concordam) < minimo:
        res.reason = (f"janelas discordam: so {len(concordam)}/{len(uteis)} dentro de "
                      f"{consenso_ms}ms da mediana (minimo {minimo}) lags={lags}, "
                      f"nenhum delay unico serve")
    elif res.spread_ms > spread_max:
        res.reason = (f"janelas concordantes ainda espalhadas: {res.spread_ms}ms "
                      f"(teto {spread_max}) lags={concordam}")
    elif res.residual_ms > early:
        res.reason = f"dub adiantado em {res.residual_ms}ms (teto {early})"
    elif res.residual_ms < -late:
        res.reason = f"dub atrasado em {-res.residual_ms}ms (teto {late})"
    else:
        # So as janelas do consenso mandam aqui. A dissidente ja foi julgada
        # acima, quando se decidiu que ela e minoria.
        fora = [l for l in concordam if l > early or l < -late]
        if fora:
            res.reason = (f"mediana {res.residual_ms}ms dentro do limite mas "
                          f"janela(s) fora: {fora}")
        else:
            res.ok = True
            if dissidentes:
                res.reason = (f"ok, ignorando {len(dissidentes)} janela(s) "
                              f"discordante(s): {dissidentes}")
    return res
