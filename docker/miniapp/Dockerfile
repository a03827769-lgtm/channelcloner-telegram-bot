# ============================================================
# ChannelCloner Pro — PHP 8.3-FPM Production Dockerfile
# Optimized for High Concurrency, FastCGI, Security & OPcache
# ============================================================

FROM php:8.3-fpm-alpine

# Install system dependencies & PHP extensions
RUN apk add --no-cache \
    curl \
    libpng-dev \
    libzip-dev \
    oniguruma-dev \
    tzdata \
    && docker-php-ext-install -j$(nproc) \
    pdo \
    pdo_mysql \
    opcache \
    mbstring \
    bcmath

# Set Timezone
ENV TZ=Asia/Tashkent
RUN cp /usr/share/zoneinfo/Asia/Tashkent /etc/localtime && echo "Asia/Tashkent" > /etc/timezone

# Copy Custom PHP & OPcache configuration
RUN { \
    echo 'opcache.memory_consumption=128'; \
    echo 'opcache.interned_strings_buffer=8'; \
    echo 'opcache.max_accelerated_files=4000'; \
    echo 'opcache.revalidate_freq=2'; \
    echo 'opcache.fast_shutdown=1'; \
    echo 'opcache.enable=1'; \
    echo 'upload_max_filesize=20M'; \
    echo 'post_max_size=20M'; \
    echo 'memory_limit=256M'; \
    echo 'date.timezone=Asia/Tashkent'; \
} > /usr/local/etc/php/conf.d/custom.ini

WORKDIR /var/www/html

# Copy application files
COPY miniapp /var/www/html

# Set permissions
RUN chown -R www-data:www-data /var/www/html

EXPOSE 9000

CMD ["php-fpm"]
