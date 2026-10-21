# -*- coding: utf-8 -*-
"""Que ninguna llamada a Hacienda pueda colgarse para siempre.

EL PROBLEMA QUE ESTAS PRUEBAS CUIDAN
requests sin timeout espera indefinidamente. Si Hacienda acepta la conexion y
despues no contesta, el worker de Odoo queda colgado con su transaccion de
PostgreSQL abierta hasta que el vigilante lo mata a los 120 segundos. Con 3
workers compartidos entre mas de 10 bases, tres usuarios esperando a Hacienda
dejan el servidor entero sin responder.

Paso el 20-oct-2026 a las 13:36:31.

LA PRUEBA PRINCIPAL ES ESTATICA, A PROPOSITO
No basta con arreglar las seis llamadas de hoy: hay que impedir que manana
alguien agregue una septima sin timeout. test_toda_llamada_a_requests_lleva_
timeout lee el archivo con el modulo ast y revisa TODAS las llamadas a
requests, incluidas las que todavia no existen. Es la unica forma de que la
prueba siga sirviendo cuando el modulo crezca.

No tocan la red ni la base de datos.
"""
import ast
import inspect
import os

from odoo.tests import common

from ..models import api_facturae


# Lo que se considera una llamada que sale a internet.
METODOS_DE_RED = {'get', 'post', 'put', 'patch', 'delete', 'head', 'request'}


def _arbol(ruta):
    with open(ruta, encoding='utf-8') as fh:
        return ast.parse(fh.read(), filename=ruta)


def _llamadas_a_requests(arbol):
    """Todas las llamadas a requests.<metodo>(...) del arbol, con su linea.

    Recibe el arbol ya parseado, no la ruta: quien compare nodos entre dos
    recorridos tiene que trabajar sobre EL MISMO arbol, porque ast crea
    objetos nuevos en cada parseo y los id() no coincidirian.
    """
    encontradas = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        f = nodo.func
        if (isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == 'requests'
                and f.attr in METODOS_DE_RED):
            encontradas.append((nodo.lineno, f.attr, nodo))
    return encontradas


class TestTimeoutHacienda(common.TransactionCase):

    def setUp(self):
        super().setUp()
        self.ruta = inspect.getsourcefile(api_facturae)

    # ------------------------------------------------------------------ 1
    def test_toda_llamada_a_requests_lleva_timeout(self):
        """Ninguna llamada a internet puede quedar sin tope de espera.

        Esta es la prueba que importa: vale para las seis de hoy y para
        cualquiera que se agregue despues.
        """
        sin_timeout = [
            'linea %d: requests.%s(...)' % (linea, metodo)
            for linea, metodo, nodo in _llamadas_a_requests(_arbol(self.ruta))
            if not any(kw.arg == 'timeout' for kw in nodo.keywords)
        ]
        self.assertFalse(
            sin_timeout,
            'Hay llamadas a Hacienda sin timeout. Sin tope de espera, una '
            'Hacienda que no contesta cuelga el worker hasta que Odoo lo mata '
            'a los 120s, y con 3 workers eso deja sin servicio a todas las '
            'bases del servidor.\n  ' + '\n  '.join(sin_timeout))

    # ------------------------------------------------------------------ 2
    def test_hay_al_menos_las_seis_llamadas_conocidas(self):
        """Si alguien borra las llamadas, la prueba 1 pasaria en el vacio.

        Este control evita ese falso verde: si el archivo deja de tener las
        seis llamadas conocidas, es que cambio algo de fondo y hay que mirarlo.
        """
        self.assertGreaterEqual(
            len(_llamadas_a_requests(_arbol(self.ruta))), 6,
            'Se esperaban al menos 6 llamadas a Hacienda y hay menos: el '
            'archivo cambio de forma y la prueba de timeouts quedo sin objeto.')

    # ------------------------------------------------------------------ 3
    def test_el_tope_es_razonable(self):
        """(conectar, responder) con valores que sirvan de verdad.

        Muy corto corta envios validos; muy largo no resuelve el problema.
        """
        tope = api_facturae.TIEMPO_ESPERA_HACIENDA
        self.assertIsInstance(tope, tuple)
        self.assertEqual(len(tope), 2, 'debe ser (conectar, responder)')
        conectar, responder = tope
        self.assertTrue(0 < conectar <= 10,
                        'conectar: entre 1 y 10 segundos, es %s' % conectar)
        self.assertTrue(10 <= responder <= 60,
                        'responder: entre 10 y 60 segundos, es %s' % responder)
        self.assertLess(responder, 120,
                        'debe vencer ANTES del limit_time_real de Odoo (120s), '
                        'o el worker muere igual y el arreglo no sirve de nada')

    # ------------------------------------------------------------------ 4
    def test_el_timeout_llega_de_verdad_a_requests(self):
        """La constante se usa, no quedo declarada y olvidada."""
        recibido = {}

        class RespuestaFalsa:
            status_code = 200
            headers = {}
            text = '{}'

            def json(self):
                return {'access_token': 'x', 'expires_in': 300,
                        'refresh_expires_in': 1800}

        def request_falso(*args, **kwargs):
            recibido['timeout'] = kwargs.get('timeout')
            return RespuestaFalsa()

        original = api_facturae.requests.request
        api_facturae.requests.request = request_falso
        try:
            api_facturae.refresh_token_hacienda('api-stag', 'token-de-prueba')
        except Exception:
            # Lo que interesa es con que se llamo, no que termine bien.
            pass
        finally:
            api_facturae.requests.request = original

        self.assertEqual(
            recibido.get('timeout'), api_facturae.TIEMPO_ESPERA_HACIENDA,
            'refresh_token_hacienda no le paso el timeout a requests')

    # ------------------------------------------------------------------ 5
    def test_un_timeout_se_comporta_como_error_de_red(self):
        """Timeout hereda de RequestException: el try/except que ya existe
        alrededor de las seis llamadas lo atrapa igual que una caida de red.

        Es lo que hace que este arreglo no cambie el camino del error.
        """
        import requests as req
        self.assertTrue(
            issubclass(req.exceptions.Timeout, req.exceptions.RequestException))
        self.assertTrue(
            issubclass(req.exceptions.ConnectTimeout, req.exceptions.Timeout))
        self.assertTrue(
            issubclass(req.exceptions.ReadTimeout, req.exceptions.Timeout))

    # ------------------------------------------------------------------ 6
    def test_cada_llamada_esta_protegida_por_un_except(self):
        """Un timeout sin quien lo atrape seria un error 500 al usuario.

        Se comprueba que cada llamada a requests viva dentro de un try que
        contemple requests.exceptions.RequestException (o Exception).
        """
        arbol = _arbol(self.ruta)   # UN solo parseo: ver nota en _llamadas_a_requests

        protegidas = set()
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Try):
                continue
            atrapa = False
            for manejador in nodo.handlers:
                t = manejador.type
                if t is None:
                    atrapa = True
                elif isinstance(t, ast.Name) and t.id == 'Exception':
                    atrapa = True
                elif isinstance(t, ast.Attribute) and t.attr in (
                        'RequestException', 'Timeout', 'ConnectionError'):
                    atrapa = True
            if not atrapa:
                continue
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.Call):
                    protegidas.add(id(hijo))

        sueltas = ['linea %d: requests.%s(...)' % (linea, metodo)
                   for linea, metodo, nodo in _llamadas_a_requests(arbol)
                   if id(nodo) not in protegidas]
        self.assertFalse(
            sueltas,
            'Hay llamadas a Hacienda fuera de un try/except: un timeout ahi '
            'le saldria al usuario como error 500.\n  ' + '\n  '.join(sueltas))


class TestIntervalosDeCron(common.TransactionCase):
    """Cada cuanto se habla con Hacienda.

    Acordado con el cliente el 21-oct-2026, despues de la caida del 20:
    enviar seguido (que el comprobante salga rapido) y consultar espaciado
    (el estado puede esperar). Cada consulta es una llamada a internet, y
    cada llamada es un worker ocupado: con 3 workers compartidos entre mas
    de diez bases, consultar cada 5 minutos en todas es carga que no paga.

    Estos valores viven en data/ir_cron_data.xml, que NO lleva noupdate:
    actualizar el modulo los vuelve a aplicar en todas las bases. Esta
    prueba es el candado para que no se cambien por descuido.
    """

    ESPERADO = {
        'ir_cron_FECR_Envia':    (5,  'minutes'),
        'ir_cron_FECR_Consulta': (15, 'minutes'),
    }

    def test_los_intervalos_son_los_acordados(self):
        for xmlid, (numero, tipo) in self.ESPERADO.items():
            cron = self.env.ref('cr_electronic_invoice.%s' % xmlid,
                                raise_if_not_found=False)
            self.assertTrue(cron, 'no existe el cron %s' % xmlid)
            self.assertEqual(
                (cron.interval_number, cron.interval_type), (numero, tipo),
                '%s deberia correr cada %s %s y corre cada %s %s'
                % (xmlid, numero, tipo, cron.interval_number, cron.interval_type))

    def test_se_envia_mas_seguido_de_lo_que_se_consulta(self):
        """La regla de fondo, por si manana se tocan los numeros."""
        envia = self.env.ref('cr_electronic_invoice.ir_cron_FECR_Envia')
        consulta = self.env.ref('cr_electronic_invoice.ir_cron_FECR_Consulta')
        self.assertEqual(envia.interval_type, consulta.interval_type)
        self.assertLess(
            envia.interval_number, consulta.interval_number,
            'enviar tiene que ser mas frecuente que consultar: el comprobante '
            'debe salir rapido, el estado puede esperar')
