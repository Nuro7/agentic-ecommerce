<?php
if (! defined('ABSPATH')) {
    exit;
}
?>
<div class="wrap wooagent-admin-wrap">
    <h1><?php esc_html_e('WooAgent Settings', 'wooagent'); ?></h1>
    <p><?php esc_html_e('Configure your AI shopping assistant and backend integration.', 'wooagent'); ?></p>

    <?php settings_errors(WOOAGENT_OPTION_KEY); ?>

    <form method="post" action="options.php">
        <?php
        settings_fields('wooagent_settings_group');
        do_settings_sections(WooAgent_Settings::PAGE_SLUG);
        submit_button(__('Save Settings', 'wooagent'));
        ?>
    </form>

    <div class="wooagent-panel">
        <h2><?php esc_html_e('Connection Test', 'wooagent'); ?></h2>
        <p><?php esc_html_e('Verify that your backend is reachable from WordPress by calling /health.', 'wooagent'); ?></p>
        <button type="button" class="button button-secondary" id="wooagent-test-connection">
            <?php esc_html_e('Test Connection', 'wooagent'); ?>
        </button>
        <div id="wooagent-test-result" aria-live="polite"></div>
    </div>

    <div class="wooagent-panel">
        <h2><?php esc_html_e('WooCommerce API Key Setup', 'wooagent'); ?></h2>
        <ol>
            <li><?php esc_html_e('Go to WooCommerce -> Settings -> Advanced -> REST API.', 'wooagent'); ?></li>
            <li><?php esc_html_e('Click "Add key" and set Description to "WooAgent Backend".', 'wooagent'); ?></li>
            <li><?php esc_html_e('Set User to an admin account and Permissions to Read/Write.', 'wooagent'); ?></li>
            <li><?php esc_html_e('Copy the Consumer Key and Consumer Secret into your backend .env file.', 'wooagent'); ?></li>
        </ol>
    </div>
</div>

<script>
(function() {
    var button = document.getElementById('wooagent-test-connection');
    var result = document.getElementById('wooagent-test-result');
    if (!button || !result) {
        return;
    }

    button.addEventListener('click', function() {
        button.disabled = true;
        result.textContent = 'Testing...';

        var data = new FormData();
        data.append('action', 'wooagent_test_connection');
        data.append('nonce', '<?php echo esc_js(wp_create_nonce('wooagent_test_connection')); ?>');

        fetch(ajaxurl, {
            method: 'POST',
            credentials: 'same-origin',
            body: data
        })
            .then(function(response) { return response.json(); })
            .then(function(payload) {
                if (payload.success) {
                    result.className = 'success';
                    result.textContent = payload.data.message;
                } else {
                    result.className = 'error';
                    result.textContent = payload.data && payload.data.message ? payload.data.message : 'Connection failed.';
                }
            })
            .catch(function() {
                result.className = 'error';
                result.textContent = 'Unexpected error while testing connection.';
            })
            .finally(function() {
                button.disabled = false;
            });
    });
})();
</script>
