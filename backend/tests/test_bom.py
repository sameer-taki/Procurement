from app.gateway.bom import (
    BomNode, explode, net_requirements, round_to_moq, explode_and_net,
)

# FINISHED = 1x BOX (+board w/ 5% scrap) + 1x LABEL (+substrate)
BOMS = {
    "FIN": (1.0, [BomNode("BOX", 1.0), BomNode("LBL", 1.0)]),
    "BOX": (1.0, [BomNode("BOARD", 0.62, 0.05)]),
    "LBL": (1.0, [BomNode("SUB", 0.04)]),
}


def test_explode_two_levels():
    gross = explode("FIN", 10000, BOMS.get)
    assert round(gross["BOARD"]) == round(10000 * 0.62 * 1.05)   # 6510
    assert round(gross["SUB"]) == 400


def test_cycle_guard():
    cyc = {"A": (1.0, [BomNode("B", 1)]), "B": (1.0, [BomNode("A", 1)])}
    try:
        explode("A", 1, cyc.get)
        assert False, "expected cycle error"
    except ValueError:
        pass


def test_net_and_moq():
    net = net_requirements({"BOARD": 6510.0}, lambda m: (1000.0, 0.0, 0.0))
    assert round(net["BOARD"]) == 5510
    sug = round_to_moq(net, lambda m: 500.0)
    assert sug["BOARD"] == 6000  # ceil(5510/500)*500


def test_pipeline():
    out = explode_and_net(
        [("FIN", 10000)],
        BOMS.get,
        lambda m: (0.0, 0.0, 0.0),
        lambda m: 0.0,
    )
    assert round(out["BOARD"]) == 6510
