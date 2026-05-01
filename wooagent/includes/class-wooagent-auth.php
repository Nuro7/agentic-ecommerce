<?php

if (! defined('ABSPATH')) {
    exit;
}

class WooAgent_Auth {
    const SIGNATURE_HEADER = 'x_wooagent_signature';
    const TIMESTAMP_HEADER = 'x_wooagent_timestamp';

    /**
     * Returns configured shared secret.
     *
     * @return string
     */
    public function get_shared_secret() {
        $options = get_option(WOOAGENT_OPTION_KEY, array());
        return isset($options['api_secret']) ? (string) $options['api_secret'] : '';
    }

    /**
     * Build HMAC signature for an outbound request to the backend.
     *
     * Payload format: timestamp + "." + path + "." + body
     * Including the path prevents a valid /chat signature being replayed
     * against a different endpoint. Must match security.py compute_signature().
     *
     * @param string $payload  Raw request body
     * @param string $timestamp Unix timestamp as string
     * @param string $path     Request path, e.g. "/chat" (default empty for back-compat)
     * @return string
     */
    public function sign_payload($payload, $timestamp, $path = '') {
        $secret = $this->get_shared_secret();
        if ($secret === '') {
            return '';
        }

        return hash_hmac('sha256', $timestamp . '.' . $path . '.' . $payload, $secret);
    }

    /**
     * Validate inbound request signature or REST nonce.
     *
     * @param WP_REST_Request $request
     * @return bool
     */
    public function validate_request($request) {
        $timestamp = $request->get_header('x-wooagent-timestamp');
        $signature = $request->get_header('x-wooagent-signature');

        if (! empty($timestamp) && ! empty($signature)) {
            if (abs(time() - (int) $timestamp) > 300) {
                return false;
            }

            $body     = $request->get_body();
            $path     = parse_url($request->get_route(), PHP_URL_PATH) ?? $request->get_route();
            $expected = $this->sign_payload($body, $timestamp, $path);
            return hash_equals($expected, $signature);
        }

        $nonce = $request->get_header('x-wp-nonce');
        if (empty($nonce)) {
            $nonce = $request->get_header('x_wp_nonce');
        }
        if (empty($nonce)) {
            $nonce = $request->get_header('x-wooagent-nonce');
        }
        if (empty($nonce)) {
            $nonce = $request->get_header('x_wooagent_nonce');
        }
        if (empty($nonce)) {
            $nonce = $request->get_param('nonce');
        }

        // Frontend widget fallback when request originates from same store.
        return (bool) wp_verify_nonce($nonce, 'wp_rest') || (bool) wp_verify_nonce($nonce, 'wooagent_nonce');
    }

    /**
     * Mask email before logging.
     *
     * @param string $email
     * @return string
     */
    public function mask_email($email) {
        if (! is_email($email)) {
            return '';
        }

        $parts = explode('@', $email);
        $name = $parts[0];
        $domain = $parts[1];

        if (strlen($name) <= 2) {
            $name_masked = substr($name, 0, 1) . '*';
        } else {
            $name_masked = substr($name, 0, 2) . str_repeat('*', max(1, strlen($name) - 2));
        }

        return $name_masked . '@' . $domain;
    }
}
