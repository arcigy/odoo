FROM odoo:19.0

USER root
RUN pip3 install --no-cache-dir --break-system-packages \
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
