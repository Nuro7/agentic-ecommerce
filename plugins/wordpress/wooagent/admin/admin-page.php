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

    <?php
    $tenant_id = get_option('wooagent_tenant_id', '');
    if ($tenant_id) : ?>
    <div class="wooagent-panel" style="border-left: 4px solid #00a32a;">
        <h2><?php esc_html_e('Registration Status', 'wooagent'); ?></h2>
        <p style="color:#00a32a; font-weight:600;">
            <?php esc_html_e('Store registered with Speako.', 'wooagent'); ?>
        </p>
        <p>
            <strong><?php esc_html_e('Tenant ID:', 'wooagent'); ?></strong>
            <code><?php echo esc_html($tenant_id); ?></code>
        </p>
        <p><?php esc_html_e('The Aria widget is live on your store. Products sync automatically.', 'wooagent'); ?></p>
    </div>
    <?php else : ?>
    <div class="wooagent-panel" style="border-left: 4px solid #dba617;">
        <h2><?php esc_html_e('Registration Status', 'wooagent'); ?></h2>
        <p style="color:#dba617; font-weight:600;">
            <?php esc_html_e('Not registered yet.', 'wooagent'); ?>
        </p>
        <p><?php esc_html_e('Enter your Backend URL above and click Save Settings — registration happens automatically.', 'wooagent'); ?></p>
    </div>
    <?php endif; ?>

    <div class="wooagent-panel">
        <h2><?php esc_html_e('Connection Test', 'wooagent'); ?></h2>
        <p><?php esc_html_e('Verify that your backend is reachable from WordPress.', 'wooagent'); ?></p>
        <button type="button" class="button button-secondary" id="wooagent-test-connection">
            <?php esc_html_e('Test Connection', 'wooagent'); ?>
        </button>
        <div id="wooagent-test-result" aria-live="polite"></div>
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
