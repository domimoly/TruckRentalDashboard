-- Ejecutar conectado como ALUMNO (XEPDB1), DESPUES de crear las tablas del taller
-- con el script de la clase 1_1_6_crea_pobla_tablas_bd_TRUCK_RENTAL.sql.
-- Antes de correrlo comprueba:  SELECT USER FROM dual;   (debe responder ALUMNO)

CREATE SEQUENCE SEQ_MULTA START WITH 1;

CREATE TABLE MULTA (
  id_multa     NUMBER(7)    NOT NULL CONSTRAINT PK_MULTA PRIMARY KEY,
  id_arriendo  NUMBER(7)    NOT NULL,
  tipo         VARCHAR2(10) NOT NULL CONSTRAINT CHK_MULTA_TIPO CHECK (tipo IN ('RETRASO','DAÑO','GRUA')),
  monto        NUMBER(8)    NOT NULL CONSTRAINT CHK_MULTA_MONTO CHECK (monto > 0),
  estado       VARCHAR2(10) DEFAULT 'PENDIENTE' NOT NULL CONSTRAINT CHK_MULTA_ESTADO CHECK (estado IN ('PENDIENTE','PAGADA','CONDONADA')),
  dias_retraso NUMBER(3),
  descripcion  VARCHAR2(100),
  CONSTRAINT FK_MULTA_ARRIENDO FOREIGN KEY (id_arriendo) REFERENCES ARRIENDO_CAMION (id_arriendo)
);

CREATE OR REPLACE TRIGGER trg_valida_fecha_devolucion
BEFORE INSERT OR UPDATE ON ARRIENDO_CAMION
FOR EACH ROW
BEGIN
  IF :NEW.fecha_devolucion IS NOT NULL AND :NEW.fecha_devolucion < :NEW.fecha_ini_arriendo THEN
    RAISE_APPLICATION_ERROR(-20001, 'La fecha de devolución no puede ser anterior al inicio del arriendo.');
  END IF;
END;
/

DECLARE
  n NUMBER;
BEGIN
  SELECT COUNT(*) INTO n FROM MULTA;
  IF n = 0 THEN
    INSERT INTO MULTA (id_multa, id_arriendo, tipo, monto, estado, dias_retraso)
    SELECT SEQ_MULTA.NEXTVAL, id_arriendo, 'RETRASO', 30000, 'PENDIENTE', 3 FROM ARRIENDO_CAMION WHERE id_arriendo = 1;

    INSERT INTO MULTA (id_multa, id_arriendo, tipo, monto, estado, descripcion)
    SELECT SEQ_MULTA.NEXTVAL, id_arriendo, 'DAÑO', 50000, 'PAGADA', 'Rayón en puerta lateral' FROM ARRIENDO_CAMION WHERE id_arriendo = 1;

    INSERT INTO MULTA (id_multa, id_arriendo, tipo, monto, estado, dias_retraso)
    SELECT SEQ_MULTA.NEXTVAL, id_arriendo, 'RETRASO', 20000, 'PENDIENTE', 2 FROM ARRIENDO_CAMION WHERE id_arriendo = 3;

    INSERT INTO MULTA (id_multa, id_arriendo, tipo, monto, estado, descripcion)
    SELECT SEQ_MULTA.NEXTVAL, id_arriendo, 'GRUA', 60000, 'PENDIENTE', 'Traslado en grúa por pana' FROM ARRIENDO_CAMION WHERE id_arriendo = 7;

    INSERT INTO MULTA (id_multa, id_arriendo, tipo, monto, estado, descripcion)
    SELECT SEQ_MULTA.NEXTVAL, id_arriendo, 'DAÑO', 25000, 'CONDONADA', 'Espejo retrovisor quebrado' FROM ARRIENDO_CAMION WHERE id_arriendo = 5;

    COMMIT;
  END IF;
END;
/

-- Resultado esperado: 5 multas; pendientes = 1 GRUA de 60.000 + 2 RETRASO por 50.000 = 110.000
SELECT tipo, estado, COUNT(*) AS cantidad, SUM(monto) AS total
FROM MULTA
GROUP BY tipo, estado
ORDER BY tipo, estado;
