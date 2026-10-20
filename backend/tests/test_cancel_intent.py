import unittest

from hermeshq.services.cancel_intent import is_cancel_request


class TestCancelIntent(unittest.TestCase):
    def test_plain_stop_words(self):
        for text in ["para", "Para", "PARA!", "pará", "parar", "stop", "STOP", "basta", "alto", "detente", "detén"]:
            self.assertTrue(is_cancel_request(text), text)

    def test_cancel_variants(self):
        for text in [
            "cancela",
            "cancélalo",
            "cancelala",
            "cancelar",
            "cancela por favor",
            "cancela eso",
            "cancela todo",
        ]:
            self.assertTrue(is_cancel_request(text), text)

    def test_phrases_like_the_real_message(self):
        for text in [
            "para , deja de hace rlo que estas haciendo",
            "deja de hacer lo que estás haciendo",
            "deja de hacer eso",
            "no sigas",
            "ya no sigas por favor",
            "para ya, esto está tardando mucho",
            "detén todo ahora",
            "cancela la tarea en curso",
            "aborta el proceso",
            "interrumpe eso",
            "olvídalo",
        ]:
            self.assertTrue(is_cancel_request(text), text)

    def test_para_as_preposition_is_not_a_cancel(self):
        for text in [
            "para mañana a las 10",
            "Para esta presentación usa la skill rápida",
            "necesito una reunión para el lunes",
            "para hoy",
            "dame el html para revisarlo",
        ]:
            self.assertFalse(is_cancel_request(text), text)

    def test_cancelling_other_things_goes_to_the_agent(self):
        for text in [
            "cancela la reunión de mañana",
            "cancela la tarea programada",
            "cancela el correo",
            "elimina la reu nomas, cancelala",
            "cancela la presentación del viernes",
        ]:
            self.assertFalse(is_cancel_request(text), text)

    def test_normal_requests_are_not_cancels(self):
        for text in [
            "dame el html",
            "olvida todo, partamos desde cero",
            "no dejalo así nomas",
            "y donde esta el html nuevo ?",
            "hola",
            "gracias",
            "",
            None,
        ]:
            self.assertFalse(is_cancel_request(text), text)

    def test_long_messages_are_never_cancels(self):
        long_text = "para " + "palabra " * 30
        self.assertFalse(is_cancel_request(long_text))
