"""Degrau de offset: dissidentes fortes reprovam, ruido nao.

Regressao dos 15 arquivos que vazaram pelo gate entre 16 e 20/08/2026, todos
com um trecho do episodio em offset proprio e a maioria das janelas boa.
"""
import pytest

from src.verify import _dissidentes_fortes


class _W:
    def __init__(self, lag_ms, peak_ratio):
        self.lag_ms = lag_ms
        self.peak_ratio = peak_ratio


def _bimodal(pares, mediana, minimo=2):
    uteis = [_W(l, r) for l, r in pares]
    fortes = _dissidentes_fortes(uteis, mediana, consenso_ms=25,
                                 dist_min=60, razao_forte=3.0)
    return len(fortes) >= minimo


@pytest.mark.parametrize("nome,pares,mediana", [
    ("one piece s07e30", [(-834, 8.1), (-833, 7.4), (0, 12), (1, 9), (1, 11), (134, 2.2)], 1),
    ("one piece s07e38", [(-366, 10.6), (-365, 8.5), (2, 11), (2, 13), (3, 22)], 2),
    ("saint seiya s03e01", [(-617, 31), (-150, 48), (-155, 37), (2, 43), (0, 29)], 0),
])
def test_degrau_reprova(nome, pares, mediana):
    assert _bimodal(pares, mediana), nome


@pytest.mark.parametrize("nome,pares,mediana", [
    # uma janela solta e sem pico: e ruido, a mediana existe para descarta-la
    ("outlier fraco", [(39, 20), (39, 18), (40, 22), (40, 19), (320, 1.9)], 40),
    # dissidentes logo depois da fronteira do consenso, dentro do audivel
    ("ruido de fronteira", [(0, 9), (1, 8), (1, 7), (35, 4), (40, 3.5)], 1),
    # um dissidente forte sozinho nao caracteriza patamar
    ("um forte so", [(0, 9), (1, 8), (1, 7), (1, 9), (-900, 15)], 1),
    ("tudo concorda", [(0, 9), (1, 8), (1, 7), (1, 9), (2, 10)], 1),
])
def test_ruido_nao_reprova(nome, pares, mediana):
    assert not _bimodal(pares, mediana), nome
