-- Corrige la jornada semanal de Sanidad Privada.
--
-- El generador uso 37,5 h como jornada completa del convenio CC-SAN-PRIV-25,
-- pero el convenio fija 37,00. Un contrato no puede superar la jornada maxima
-- de su convenio, asi que los 16 contratos a jornada completa estaban media
-- hora por encima del tope, y los dos parciales tenian el FTE calculado sobre
-- una jornada completa equivocada.
--
-- Se reescala manteniendo la PROPORCION contratada, no las horas: quien tenia
-- media jornada sigue teniendo media jornada. Por eso el FTE es la cifra que
-- se conserva y las horas se derivan de el, y no al reves.
--
-- El convenio vive en collective_agreements_db, asi que 37,0 va aqui como
-- literal: no hay forma de cruzar las dos bases desde un solo script. Si el
-- convenio cambia su jornada, este numero hay que cambiarlo con el.
--
-- Idempotente: solo toca los contratos cuya jornada no cuadra ya, asi que una
-- segunda ejecucion no hace nada.

BEGIN;

UPDATE contracts
   SET weekly_hours = ROUND(37.0 * (weekly_hours / 37.5), 2),
       fte          = ROUND(weekly_hours / 37.5, 2),
       updated_at   = now()
 WHERE tenant_id = 'acme'
   AND collective_agreement_id = '22222222-2222-2222-2222-222222222222'
   AND weekly_hours <> ROUND(37.0 * (weekly_hours / 37.5), 2);

COMMIT;

-- Ninguna jornada debe superar las 37,00 del convenio.
SELECT weekly_hours, fte, count(*) AS contratos
  FROM contracts
 WHERE tenant_id = 'acme'
   AND collective_agreement_id = '22222222-2222-2222-2222-222222222222'
 GROUP BY 1, 2
 ORDER BY 2 DESC;
