<?php
require_once 'wp-load.php';
$options = get_option('wooagent_settings', array());
echo "SECRET: " . (isset($options['api_secret']) ? $options['api_secret'] : 'NOT SET') . "\n";
echo "BACKEND: " . (isset($options['backend_url']) ? $options['backend_url'] : 'NOT SET') . "\n";
