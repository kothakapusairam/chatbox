import os
import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import joblib
import numpy as np
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.metrics import (
    classification_report,
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    confusion_matrix
)

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Expected minimum intent categories
REQUIRED_INTENTS = [
    "greeting",
    "goodbye",
    "thanks",
    "order_status",
    "order_tracking",
    "cancellation",
    "return_request",
    "refund_request",
    "payment_issue",
    "make_payment",
    "delivery_delay",
    "damaged_product",
    "product_information",
    "account_access",
    "password_reset",
    "account_update",
    "shipping_information",
    "warranty_information",
    "complaint",
    "human_support",
    "unknown_query"
]


def build_pipeline(random_state=42):
    """Word + character n-gram TF-IDF (robust to typos and short phrases) feeding a balanced logistic regression."""
    features = FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, strip_accents="unicode", lowercase=True)),
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, lowercase=True)),
    ])
    return Pipeline([
        ("features", features),
        ("clf", LogisticRegression(C=10.0, max_iter=3000, random_state=random_state,
                                   class_weight="balanced", solver="lbfgs")),
    ])

def load_and_validate_intents(json_path):
    """
    Loads intents.json and validates its structure, presence of required categories,
    and example counts.
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Intents file not found at: {json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "intents" not in data or not isinstance(data["intents"], list):
        raise ValueError("Invalid format: 'intents' key must contain a list of intent objects.")

    found_intents = {}
    texts = []
    labels = []

    for item in data["intents"]:
        intent_tag = item.get("intent")
        patterns = item.get("patterns", [])
        if not intent_tag:
            continue

        clean_patterns = [p.strip() for p in patterns if isinstance(p, str) and p.strip()]
        found_intents[intent_tag] = len(clean_patterns)

        for pat in clean_patterns:
            texts.append(pat)
            labels.append(intent_tag)

    # Check for missing required intents
    missing = [req for req in REQUIRED_INTENTS if req not in found_intents]
    if missing:
        logger.warning(f"Warning: The following expected intents were missing from dataset: {missing}")
    else:
        logger.info("All %d required intent categories present and accounted for.", len(REQUIRED_INTENTS))

    logger.info(f"Loaded {len(texts)} total training examples across {len(found_intents)} intents.")
    return texts, labels, found_intents

def train_and_evaluate(texts, labels, models_dir, random_state=42):
    """
    Splits dataset into stratified train and test sets, trains a TF-IDF + LogisticRegression
    pipeline, evaluates metrics, and persists the pipeline and metadata.
    """
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("      AI CUSTOMER SUPPORT CHATBOT - MODEL TRAINING & EVALUATION      ")
    print("=" * 70)

    # Stratified Train/Test split
    X_train, X_test, y_train, y_test = train_test_split(
        texts,
        labels,
        test_size=0.25,
        random_state=random_state,
        stratify=labels
    )

    print(f"Dataset split: {len(X_train)} training samples, {len(X_test)} held-out evaluation samples.")

    # Scikit-learn Pipeline
    pipeline = build_pipeline(random_state)

    # Train on training partition
    pipeline.fit(X_train, y_train)

    # Evaluate on held-out test partition
    y_pred = pipeline.predict(X_test)

    acc = accuracy_score(y_test, y_pred)
    prec_macro = precision_score(y_test, y_pred, average="macro", zero_division=0)
    rec_macro = recall_score(y_test, y_pred, average="macro", zero_division=0)
    f1_macro = f1_score(y_test, y_pred, average="macro", zero_division=0)
    f1_weighted = f1_score(y_test, y_pred, average="weighted", zero_division=0)

    print("\n--- HELD-OUT TEST EVALUATION METRICS ---")
    print(f"Accuracy:        {acc * 100:.2f}%")
    print(f"Macro Precision: {prec_macro:.4f}")
    print(f"Macro Recall:    {rec_macro:.4f}")
    print(f"Macro F1-Score:  {f1_macro:.4f}")
    print(f"Weighted F1:     {f1_weighted:.4f}")
    print("\n--- CLASSIFICATION REPORT ---")
    print(classification_report(y_test, y_pred, zero_division=0))

    # Stratified 5-fold cross-validation: a more stable accuracy estimate than one split.
    cv_scores = cross_val_score(build_pipeline(random_state), texts, labels,
                                cv=StratifiedKFold(n_splits=5, shuffle=True, random_state=random_state))
    print(f"5-fold cross-validation accuracy: {cv_scores.mean() * 100:.2f}% (+/- {cv_scores.std() * 100:.2f})")

    # Now fit the pipeline on the full dataset so the production model benefits
    # from all available examples
    print("\nFitting final deployment pipeline on 100% of labeled data...")
    full_pipeline = build_pipeline(random_state)
    full_pipeline.fit(texts, labels)

    # Model and metadata persistence
    model_file = models_dir / "intent_model.joblib"
    metadata_file = models_dir / "model_metadata.json"

    joblib.dump(full_pipeline, model_file)
    logger.info(f"Model saved successfully to: {model_file}")

    metadata = {
        "model_version": "1.0.0",
        "model_architecture": "FeatureUnion(TF-IDF word 1-2gram + TF-IDF char_wb 3-5gram) + LogisticRegression(C=10, balanced)",
        "training_timestamp": datetime.now(timezone.utc).isoformat(),
        "total_samples": len(texts),
        "total_classes": len(full_pipeline.classes_),
        "classes": list(full_pipeline.classes_),
        "test_split_ratio": 0.25,
        "evaluation_metrics": {
            "accuracy": round(float(acc), 4),
            "macro_precision": round(float(prec_macro), 4),
            "macro_recall": round(float(rec_macro), 4),
            "macro_f1": round(float(f1_macro), 4),
            "weighted_f1": round(float(f1_weighted), 4)
        },
        "cross_validation": {
            "folds": cv_scores.shape[0],
            "accuracy_mean": round(float(cv_scores.mean()), 4),
            "accuracy_std": round(float(cv_scores.std()), 4),
        },
        "recommended_confidence_threshold": 0.40
    }

    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    logger.info(f"Metadata saved successfully to: {metadata_file}")
    print("\n Training & Serialization Complete!")
    print(f"Model path:    {model_file}")
    print(f"Metadata path: {metadata_file}")
    print("=" * 70)

    return full_pipeline, metadata

if __name__ == "__main__":
    from config import Config
    intents_file = Config.INTENTS_PATH
    models_dir = Path(Config.MODEL_PATH).parent
    texts, labels, intent_counts = load_and_validate_intents(intents_file)
    train_and_evaluate(texts, labels, models_dir)

