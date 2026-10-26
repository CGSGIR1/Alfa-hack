# TLS для публичных курсов Альфа-Банка

`russian_trusted_root_ca.pem` получен 25.10.2026 с официального CDN Госуслуг:
https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer
Официальная страница сертификатов: https://www.gosuslugi.ru/crt

Сертификат используется только для фиксированного HTTPS-запроса к
`https://alfabank.ru/api/v1/scrooge/currencies/alfa-rates`.
Проверяются цепочка доверия, имя хоста и срок действия. Перенаправления на другой адрес запрещены.
Системное и браузерное хранилища доверия не изменяются; LLM и остальные
сетевые запросы используют прежние настройки TLS.

SHA-256 (содержимое PEM-файла): 936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504
