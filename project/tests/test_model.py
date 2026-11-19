import json
from pathlib import Path

import pytest

import chatbot as chatbot_module
from config import BASE_DIR

MODEL = BASE_DIR / "models" / "intent_model.joblib"
META = BASE_DIR / "models" / "model_metadata.json"


@pytest.fixture(scope="module")
def engine():
    return chatbot_module.ChatbotEngine(model_path=str(MODEL),
                                        knowledge_base_path=str(BASE_DIR / "knowledge_base.json"),
                                        confidence_threshold=0.40)


def test_model_artefacts_exist():
    assert MODEL.exists() and META.exists()


def test_recorded_quality_meets_gate():
    """Gate on 5-fold cross-validated accuracy (stable), and on the held-out macro F1."""
    meta = json.loads(Path(META).read_text(encoding="utf-8"))
    assert meta["cross_validation"]["accuracy_mean"] >= 0.82
    assert meta["evaluation_metrics"]["macro_f1"] >= 0.80


@pytest.mark.parametrize("phrase,expected", [
    ("where is my package right now", "order_tracking"),
    ("I forgot my password and cannot log in", "password_reset"),
    ("I want my money back for the broken item", "refund_request"),
    ("I'd like to pay for my order please", "make_payment"),
    ("hello", "greeting"),
    ("bye, that's all for today", "goodbye"),
    ("my item arrived damaged", "damaged_product"),
    ("the warranty on this product", "warranty_information"),
])
def test_common_customer_phrasings_are_classified(engine, phrase, expected):
    intent, confidence = engine.predict_intent(phrase)
    assert intent == expected, (phrase, intent, confidence)
    assert confidence >= 0.40


def test_model_unavailable_degrades_to_unknown(tmp_path):
    engine = chatbot_module.ChatbotEngine(model_path=str(tmp_path / "missing.joblib"),
                                          knowledge_base_path=str(BASE_DIR / "knowledge_base.json"))
    assert engine.predict_intent("hello") == ("unknown_query", 0.0)
