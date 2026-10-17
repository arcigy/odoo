# Pinned to the exact Odoo build running on arcigy-odoo (server_version 19.0-20260630).
# Bump deliberately: test on a restored copy of the database before deploying.
FROM odoo:19.0-20260630@sha256:f54272f31d5f77e4146b887efb3761c98480317daf687e4b4b5e76ed8bcc08c5

USER root
COPY config/python-constraints.txt /tmp/python-constraints.txt
RUN pip3 install --no-cache-dir --break-system-packages \
    --constraint /tmp/python-constraints.txt \
    openpyxl==3.1.5 \
    google-api-python-client==2.175.0 \
    google-auth-oauthlib==1.2.2 \
    google-auth-httplib2==0.2.0
COPY addons /mnt/extra-addons
COPY config/odoo.conf /etc/odoo/odoo.conf
COPY start-odoo.sh /opt/geotherm/start-odoo.sh
RUN chown -R odoo:odoo /mnt/extra-addons /opt/geotherm && chmod +x /opt/geotherm/start-odoo.sh
USER odoo

ENTRYPOINT ["/opt/geotherm/start-odoo.sh"]
