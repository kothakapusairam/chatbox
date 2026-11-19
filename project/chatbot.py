"""
Customer chatbot engine.

Pipeline for every message:
  1. Load conversation state (pending multi-turn request, context).
  2. Pick up order IDs / email addresses from the text (or the session email).
  3. Continue a pending request if one is waiting for an order ID or an email.
  4. Classify intent with the scikit-learn model; keyword rules refine order intents.
  5. Order-related intents are only answered after the order ID AND the email
     used at checkout have been verified. Anonymous lookups never reveal order data.
  6. Otherwise: knowledge-base answer, or escalation to a human support ticket.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone

import joblib

import database
import payments
from config import Config
from utils import (EMAIL_IN_TEXT_RE, ORDER_ID_RE, emails_match, extract_email, extract_order_id,
                   format_cents, is_valid_email, new_ticket_id, parse_bare_order_id)

logger = logging.getLogger(__name__)

ORDER_INTENTS = {"order_status", "order_tracking", "cancellation", "return_request", "refund_request",
                 "make_payment"}
COLLECT_ORDER = "collect_order_id"
COLLECT_EMAIL = "collect_email"
MAX_VERIFY_FAILURES = 5

ORDER_PROMPTS = {
    "order_status": "I'd be glad to check that. Please share your **Order ID** (for example, ORD1025).",
    "order_tracking": "I can track that for you. Please share your **Order ID** (for example, ORD1025).",
    "cancellation": "I can help with cancelling. Please share the **Order ID** you want to cancel (for example, ORD1027).",
    "return_request": "We accept returns within 30 days of delivery. Please share the **Order ID** you'd like to return.",
    "refund_request": "I can check your refund status. Please share your **Order ID** (for example, ORD1025).",
    "make_payment": "I can send you a secure payment link. Please share the **Order ID** you want to pay for.",
}

PAY_PATTERN = re.compile(r"\b(pay now|pay for|pay my|pay the|checkout|check out|complete (?:my |the )?payment|"
                         r"payment link|make a payment|send (?:me )?(?:a )?payment)\b", re.IGNORECASE)
CANCEL_PATTERN = re.compile(r"\bcancel\w*\b", re.IGNORECASE)
RETURN_PATTERN = re.compile(r"\breturn\w*\b", re.IGNORECASE)
REFUND_PATTERN = re.compile(r"\brefund\w*\b", re.IGNORECASE)
TRACK_PATTERN = re.compile(r"\b(track\w*|courier|transit|carrier|where|shipped|delivery)\b", re.IGNORECASE)
EXIT_PATTERN = re.compile(r"\b(nevermind|never mind|cancel request|stop|forget it|start over)\b", re.IGNORECASE)
EXIT_BARE_PATTERN = re.compile(r"^\s*cancel\s*$", re.IGNORECASE)
URGENT_PATTERN = re.compile(r"\b(urgent|urgently|emergency|fraud|immediate|immediately|asap)\b", re.IGNORECASE)
# Safety-critical wording is escalated straight to a human, whatever the classifier thinks.
SAFETY_PATTERN = re.compile(r"\b(fraud\w*|unauthori[sz]ed|stolen|scam\w*|hacked|identity theft|emergency)\b",
                            re.IGNORECASE)

PAYMENT_LABELS = {
    "unpaid": "Not yet paid",
    "pending": "Payment in progress",
    "paid": "Paid",
    "partially_refunded": "Partially refunded",
    "refunded": "Refunded",
    "failed": "Payment failed",
}
ESCALATION_PRIORITY = {"complaint": "High", "damaged_product": "High", "payment_issue": "High"}


class ChatbotEngine:
    """ML intent classification + knowledge base + order workflows + escalation."""

    def __init__(self, model_path=None, knowledge_base_path=None, confidence_threshold=None,
                 metadata_path=None):
        self.model_path = model_path or Config.MODEL_PATH
        self.knowledge_base_path = knowledge_base_path or Config.KNOWLEDGE_BASE_PATH
        self.metadata_path = metadata_path or os.path.join(os.path.dirname(self.model_path), "model_metadata.json")
        self.confidence_threshold = (confidence_threshold if confidence_threshold is not None
                                     else Config.CONFIDENCE_THRESHOLD)
        self.pipeline = None
        self.knowledge_base = {}
        self.model_metadata = {}
        self.load_model()
        self.load_knowledge_base()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_model(self):
        if os.path.exists(self.model_path):
            try:
                self.pipeline = joblib.load(self.model_path)
                logger.info("Loaded intent model from %s", self.model_path)
            except Exception:  # corrupted artefact: degrade gracefully instead of crashing
                logger.exception("Could not load intent model from %s", self.model_path)
                self.pipeline = None
        else:
            logger.warning("Intent model not found at %s. Run 'python train_model.py'.", self.model_path)
        if os.path.exists(self.metadata_path):
            try:
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    self.model_metadata = json.load(f)
            except (OSError, json.JSONDecodeError):
                logger.exception("Could not read model metadata")

    def load_knowledge_base(self):
        if not os.path.exists(self.knowledge_base_path):
            logger.warning("Knowledge base not found at %s", self.knowledge_base_path)
            return
        try:
            with open(self.knowledge_base_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("knowledge_base", []):
                self.knowledge_base[item["intent"]] = item
            logger.info("Loaded knowledge base with %d topics", len(self.knowledge_base))
        except (OSError, json.JSONDecodeError, KeyError):
            logger.exception("Failed to load knowledge base")

    # ------------------------------------------------------------------
    # NLP helpers
    # ------------------------------------------------------------------

    @staticmethod
    def preprocess_text(text):
        if not text or not isinstance(text, str):
            return ""
        return re.sub(r"\s+", " ", text.strip())

    def predict_intent(self, user_text):
        """Return (intent, confidence). Falls back to ('unknown_query', 0.0) when unavailable."""
        if not self.pipeline:
            return "unknown_query", 0.0
        clean = self.preprocess_text(user_text)
        if not clean:
            return "unknown_query", 0.0
        try:
            probs = self.pipeline.predict_proba([clean])[0]
            idx = int(probs.argmax())
            return str(self.pipeline.classes_[idx]), float(probs[idx])
        except Exception:
            logger.exception("Intent prediction failed")
            return "unknown_query", 0.0

    def generate_ticket_id(self, conn=None):
        """Unique ticket id; checks the database when a connection is given."""
        for _ in range(10):
            ticket_id = new_ticket_id()
            if conn is None or database.get_ticket(conn, ticket_id) is None:
                return ticket_id
        raise RuntimeError("Could not allocate a unique ticket id")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def process_message(self, conn, conversation_id, user_text, customer_name="Customer",
                        customer_email=None, commerce=None):
        text = self.preprocess_text(user_text)
        if not text:
            return self._reply(conn, conversation_id, "Please enter a message so I can assist you.",
                               "unknown_query", 0.0)

        conv = database.get_conversation(conn, conversation_id)
        if not conv:
            conv = database.create_conversation(conn, conversation_id, customer_name)
        context = conv.get("context_data") or {}
        pending = conv.get("pending_intent")
        database.save_message(conn, conversation_id, "user", text)

        order_id = extract_order_id(text)
        email = extract_email(text) or (customer_email if is_valid_email(customer_email) else None)
        if email:
            email = email.strip().lower()

        if pending:
            if EXIT_PATTERN.search(text) or EXIT_BARE_PATTERN.match(text):
                database.update_conversation_context(conn, conversation_id, None, {})
                return self._reply(conn, conversation_id,
                                   "No problem, I've cleared that request. How else can I help you today?",
                                   "greeting", 1.0)
            result = self._continue_pending(conn, conversation_id, pending, text, order_id, email, context,
                                            commerce)
            if result is not None:
                return result

        if SAFETY_PATTERN.search(text):
            return self._escalate(conn, conversation_id, text, "account_access", customer_name, email,
                                  reason="Safety-critical wording detected (possible fraud or account compromise).",
                                  force_priority="Urgent")

        model_text = ORDER_ID_RE.sub("order", EMAIL_IN_TEXT_RE.sub("email", text))
        predicted, confidence = self.predict_intent(model_text)

        if order_id:
            predicted, confidence = self._order_intent_for_text(text, predicted, confidence)
        else:
            if predicted == "human_support":
                return self._escalate(conn, conversation_id, text, predicted, customer_name, email,
                                      reason="Customer explicitly requested human support assistance.")
            if confidence < self.confidence_threshold or predicted == "unknown_query":
                return self._low_confidence(conn, conversation_id, text, predicted, confidence,
                                            customer_name, email, context)

        context["low_confidence_count"] = 0
        database.update_conversation_context(conn, conversation_id, None, context)

        if predicted == "human_support":
            return self._escalate(conn, conversation_id, text, predicted, customer_name, email,
                                  reason="Customer explicitly requested human support assistance.")
        if predicted in ORDER_INTENTS:
            return self._order_flow(conn, conversation_id, predicted, order_id, email, context,
                                    commerce, confidence)
        if predicted in ("complaint", "damaged_product"):
            return self._escalate(conn, conversation_id, text, predicted, customer_name, email,
                                  reason=f"High-impact customer report ({predicted.replace('_', ' ')}).")

        return self._knowledge_base_reply(conn, conversation_id, predicted, confidence)

    # ------------------------------------------------------------------
    # Intent refinement for messages that contain an order id
    # ------------------------------------------------------------------

    def _order_intent_for_text(self, text, predicted, confidence):
        if PAY_PATTERN.search(text):
            intent = "make_payment"
        elif CANCEL_PATTERN.search(text):
            intent = "cancellation"
        elif RETURN_PATTERN.search(text):
            intent = "return_request"
        elif REFUND_PATTERN.search(text):
            intent = "refund_request"
        elif TRACK_PATTERN.search(text):
            intent = "order_tracking"
        elif predicted == "payment_issue":
            intent = "refund_request"  # payment questions about an order show its payment status
        elif predicted in ORDER_INTENTS:
            intent = predicted
        else:
            intent = "order_status"
        return intent, max(confidence, 0.9)

    # ------------------------------------------------------------------
    # Multi-turn collection of order id / email
    # ------------------------------------------------------------------

    def _continue_pending(self, conn, cid, pending, text, order_id, email, context, commerce):
        flow = context.get("flow") or {}
        intent = flow.get("intent")
        if intent not in ORDER_INTENTS:
            database.update_conversation_context(conn, cid, None, {})
            return None

        if not order_id and not email and not parse_bare_order_id(text):
            pred, conf = self.predict_intent(text)
            if conf >= self.confidence_threshold and pred not in ORDER_INTENTS and pred != "unknown_query":
                database.update_conversation_context(conn, cid, None, {})
                return None  # the customer changed topic; handle the new message normally

        oid = flow.get("order_id")
        if pending == COLLECT_ORDER:
            oid = order_id or parse_bare_order_id(text)
            if not oid:
                return self._ask(conn, cid, intent, COLLECT_ORDER, None,
                                 ORDER_PROMPTS[intent] + " I couldn't find an Order ID in that message.")
        elif order_id:
            oid = order_id

        if not oid:
            return self._ask(conn, cid, intent, COLLECT_ORDER, None, ORDER_PROMPTS[intent])
        if not email:
            return self._ask(conn, cid, intent, COLLECT_EMAIL, oid,
                             f"To protect your account, please also send the email address you used for order "
                             f"**{oid}** (for example, name@example.com).")
        return self._execute(conn, cid, intent, oid, email, context, commerce)

    def _order_flow(self, conn, cid, intent, order_id, email, context, commerce, confidence):
        if not order_id:
            return self._ask(conn, cid, intent, COLLECT_ORDER, None,
                             ORDER_PROMPTS[intent], confidence)
        if not email:
            return self._ask(conn, cid, intent, COLLECT_EMAIL, order_id,
                             f"To protect your account, please also send the email address you used for order "
                             f"**{order_id}** (for example, name@example.com).", confidence)
        return self._execute(conn, cid, intent, order_id, email, context, commerce, confidence)

    def _ask(self, conn, cid, intent, pending_key, order_id, prompt, confidence=0.9):
        flow = {"intent": intent, "order_id": order_id}
        database.update_conversation_context(conn, cid, pending_intent=pending_key,
                                             context_data={"flow": flow, "low_confidence_count": 0})
        return self._reply(conn, cid, prompt, intent, confidence)

    # ------------------------------------------------------------------
    # Verified order actions
    # ------------------------------------------------------------------

    def _execute(self, conn, cid, intent, order_id, email, context, commerce, confidence=0.95):
        context.pop("flow", None)
        order = database.get_order(conn, order_id)
        verified = bool(order) and emails_match(order.get("customer_email"), email)
        if not verified:
            failures = int(context.get("verify_failures", 0)) + 1
            context["verify_failures"] = failures
            database.update_conversation_context(conn, cid, None, context)
            if failures >= MAX_VERIFY_FAILURES:
                return self._reply(conn, cid, "For your security, we can't continue with order lookups in this "
                                              "chat. Please ask for human support and an agent will help you.",
                                   intent, confidence)
            return self._reply(conn, cid, "We couldn't verify that Order ID and email combination. Please check "
                                          "both and try again, or ask for human support.", intent, confidence)

        context["verify_failures"] = 0
        database.update_conversation_context(conn, cid, None, context)

        if intent in ("order_status", "order_tracking"):
            msg, ticket_id = self._order_summary(order), None
        elif intent == "refund_request":
            msg, ticket_id = self._refund_summary(conn, order), None
        elif intent == "cancellation":
            msg, ticket_id = self._cancel(conn, order, commerce)
        elif intent == "return_request":
            msg, ticket_id = self._return(conn, cid, order)
        elif intent == "make_payment":
            msg, ticket_id = self._pay(conn, order, commerce)
        else:
            msg, ticket_id = self._order_summary(order), None
        return self._reply(conn, cid, msg, intent, confidence, escalated=bool(ticket_id), ticket_id=ticket_id)

    def _order_summary(self, order):
        oid = order["order_id"]
        lines = [
            f"📦 **Order {oid}**",
            f"• **Status:** {order['status']}",
            f"• **Payment:** {PAYMENT_LABELS.get(order['payment_status'], order['payment_status'])}",
        ]
        if order["carrier"] and order["carrier"] != "N/A":
            tracking = f" (`{order['tracking_number']}`)" if order["tracking_number"] not in ("", "N/A") else ""
            lines.append(f"• **Carrier:** {order['carrier']}{tracking}")
        lines.append(f"• **Items:** {order['items']}")
        lines.append(f"• **Total:** {format_cents(order['amount_cents'], order['currency'])}")
        if order["estimated_delivery"] not in ("", "N/A"):
            label = "Delivered on" if order["status"] == "Delivered" else "Estimated delivery"
            lines.append(f"• **{label}:** {order['estimated_delivery']}")
        return "\n".join(lines)

    def _refund_summary(self, conn, order):
        oid = order["order_id"]
        payment = database.get_succeeded_payment(conn, oid)
        if not payment:
            pending = database.list_payments_for_order(conn, oid)
            if pending and pending[0]["status"] == "pending":
                return (f"Your payment for **{oid}** is still being processed. Once it's confirmed, "
                        f"you'll see it here.")
            return (f"No payment is recorded for **{oid}** yet. Ask me for a payment link if you'd like to pay "
                    f"for this order.")
        refunds = database.list_refunds_for_order(conn, oid)
        lines = [f"💳 **Payment for {oid}**",
                 f"• **Amount paid:** {format_cents(payment['amount_cents'], payment['currency'])}"]
        if not refunds:
            lines.append("• **Refunds:** none issued yet")
        else:
            lines.append("• **Refunds:**")
            for refund in refunds:
                lines.append(f"  – {format_cents(refund['amount_cents'], refund['currency'])}: "
                             f"{refund['status'].title()}")
        lines.append("Refunds go back to your original payment method. Bank processing times vary.")
        return "\n".join(lines)

    def _cancel(self, conn, order, commerce):
        oid = order["order_id"]
        status = order["status"]
        if status == "Processing":
            if commerce is None:
                return "Cancellations are temporarily unavailable. Please ask for human support.", None
            result = payments.cancel_order(conn, commerce.provider, commerce.config, oid, actor="chatbot")
            outcome = result["outcome"]
            if outcome == "cancelled_no_charge":
                msg = (f"✅ **Order {oid} has been cancelled.** No payment had been taken, "
                       f"so there is nothing to refund.")
            elif outcome == "cancelled_already_refunded":
                msg = f"✅ **Order {oid} has been cancelled.** It was already refunded."
            elif outcome == "cancelled_refund_succeeded":
                amount = format_cents(result["refund"]["amount_cents"], result["refund"]["currency"])
                msg = (f"✅ **Order {oid} has been cancelled.** A refund of **{amount}** has been issued to your "
                       f"original payment method.")
            elif outcome == "cancelled_refund_pending":
                amount = format_cents(result["refund"]["amount_cents"], result["refund"]["currency"])
                msg = (f"✅ **Order {oid} has been cancelled.** A refund of **{amount}** has been submitted to "
                       f"your bank. Banks usually post refunds within 5–10 business days.")
            else:
                msg = (f"✅ **Order {oid} has been cancelled.** We couldn't process the refund automatically, so "
                       f"we've opened ticket `{result['ticket_id']}` for our finance team, who will follow up.")
            return msg, result.get("ticket_id")
        if status == "In Transit":
            return (f"⚠️ **Order {oid} is already in transit** with {order['carrier']} and can no longer be "
                    f"cancelled. Once it arrives, you can request a return within 30 days of delivery."), None
        if status == "Delivered":
            return (f"Order {oid} has already been delivered, so it can't be cancelled. You can request a "
                    f"return instead."), None
        if status == "Cancelled":
            return f"Order {oid} is already cancelled.", None
        return f"Order {oid} is '{status}' and can't be cancelled automatically.", None

    def _return(self, conn, cid, order):
        oid = order["order_id"]
        status = order["status"]
        if status == "Delivered":
            if order["return_eligible"] and self._within_return_window(order["estimated_delivery"]):
                ticket_id = self.generate_ticket_id(conn)
                database.create_support_ticket(
                    conn, ticket_id=ticket_id, conversation_id=cid, customer_name=order["customer_name"],
                    customer_email=order["customer_email"],
                    issue_description=f"Return requested for {oid}: {order['items']}.",
                    predicted_category="return_request", priority="Medium", status="Open", order_id=oid,
                )
                database.add_ticket_note(conn, ticket_id, "System",
                                         "Return request created from chatbot. Verify receipt before refunding.")
                return (f"✅ **Return request received for {oid}.** Your reference is `{ticket_id}`.\n\n"
                        f"• Pack the item in its original packaging.\n"
                        f"• Our team will confirm your prepaid return label by email.\n"
                        f"• After the item is received and inspected, your refund of "
                        f"{format_cents(order['amount_cents'], order['currency'])} will be issued to your original "
                        f"payment method."), ticket_id
            return (f"Order {oid} is outside our {Config.RETURN_WINDOW_DAYS}-day return window or isn't eligible "
                    f"for returns. If you believe this is an error, or the item arrived defective, I can connect "
                    f"you with human support."), None
        if status == "Processing":
            return (f"Returns can only be requested after an order is delivered. Order {oid} hasn't shipped yet, "
                    f"so you may be able to cancel it instead."), None
        if status == "In Transit":
            return (f"Order {oid} is still in transit. You can request a return once it has been delivered."), None
        return f"Order {oid} is '{status}', so no return is needed.", None

    def _pay(self, conn, order, commerce):
        oid = order["order_id"]
        if order["payment_status"] == "paid":
            return f"Order {oid} has already been paid. Thank you!", None
        if commerce is None:
            return "Online payments are temporarily unavailable. Please ask for human support.", None
        try:
            result = payments.start_checkout(conn, commerce.provider, commerce.config, order,
                                             order.get("customer_email"), actor="chatbot")
        except payments.PaymentError as exc:
            return exc.message, None
        amount = format_cents(result["amount_cents"], result["currency"])
        return (f"💳 **Secure payment link for {oid}** ({amount})\n\n{result['checkout_url']}\n\n"
                f"The link expires in {result['expires_in_minutes']} minutes. You'll see your order as paid "
                f"once the payment is confirmed."), None

    @staticmethod
    def _within_return_window(date_text):
        try:
            delivered = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        days = (datetime.now(timezone.utc) - delivered).days
        return 0 <= days <= Config.RETURN_WINDOW_DAYS

    # ------------------------------------------------------------------
    # Knowledge base, low confidence, escalation
    # ------------------------------------------------------------------

    def _knowledge_base_reply(self, conn, cid, predicted, confidence):
        entry = self.knowledge_base.get(predicted)
        if entry:
            text = entry.get("approved_answer", "")
            instructions = entry.get("relevant_instructions", [])[:3]
            if instructions:
                text += "\n\n**Next Steps:**\n" + "\n".join(f"• {item}" for item in instructions)
        else:
            text = ("I have noted your request. Let me know if you'd like me to connect you with a human "
                    "support agent.")
        return self._reply(conn, cid, text, predicted, confidence)

    def _low_confidence(self, conn, cid, text, predicted, confidence, customer_name, email, context):
        count = int(context.get("low_confidence_count", 0)) + 1
        context["low_confidence_count"] = count
        database.update_conversation_context(conn, cid, None, context)
        if count >= 2:
            return self._escalate(conn, cid, text, "unknown_query", customer_name, email,
                                  reason="Chatbot reached retry limit without confident intent classification.")
        msg = ("I'm not completely sure I understood your question. Could you explain the issue in a little more "
               "detail? You can also click 'Human Support' at any time.")
        return self._reply(conn, cid, msg, "unknown_query", confidence)

    def _escalate(self, conn, cid, text, predicted_intent, customer_name, customer_email, reason,
                  force_priority=None):
        ticket_id = self.generate_ticket_id(conn)
        priority = ESCALATION_PRIORITY.get(predicted_intent, "Medium")
        if URGENT_PATTERN.search(text) or force_priority:
            priority = force_priority or "Urgent"
        database.create_support_ticket(
            conn, ticket_id=ticket_id, conversation_id=cid, customer_name=customer_name or "Customer",
            customer_email=customer_email, issue_description=text[:Config.MAX_DESCRIPTION_LENGTH],
            predicted_category=predicted_intent, priority=priority, status="Open",
        )
        database.add_ticket_note(conn, ticket_id, "System", f"Escalated automatically: {reason}")
        database.update_conversation_context(conn, cid, None, {})
        contact_tip = "" if customer_email else \
            "\n\nTip: share your email address with us so we can send you updates."
        msg = (f"👤 **Human Support Requested**\n\n"
               f"I have opened a support ticket for you.\n\n"
               f"• **Ticket ID**: `{ticket_id}`\n"
               f"• **Category**: {predicted_intent.replace('_', ' ').title()}\n"
               f"• **Priority**: {priority}\n"
               f"• **Status**: Open\n\n"
               f"A support specialist will review your conversation. You can quote your Ticket ID "
               f"(`{ticket_id}`) to check its status.{contact_tip}")
        return self._reply(conn, cid, msg, predicted_intent, 1.0, escalated=True, ticket_id=ticket_id)

    # ------------------------------------------------------------------
    # Persistence helper
    # ------------------------------------------------------------------

    def _reply(self, conn, cid, text, intent, confidence, escalated=False, ticket_id=None):
        confidence = float(confidence)
        message_id = database.save_message(conn, cid, "bot", text, intent, round(confidence, 4))
        return {
            "response": text,
            "intent": intent,
            "confidence": round(confidence, 4),
            "escalated": escalated,
            "ticket_id": ticket_id,
            "message_id": message_id,
        }
