-- Ejecutar conectado como SYS al servicio XEPDB1 (solo si el usuario alumno no existe).
-- Reemplaza <tu_contraseña> por la contraseña que quieras para alumno.
CREATE USER alumno IDENTIFIED BY <tu_contraseña>;
ALTER USER alumno QUOTA UNLIMITED ON USERS;
GRANT CREATE SESSION TO alumno;
GRANT RESOURCE TO alumno;
