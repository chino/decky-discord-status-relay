import { FC, useCallback, useEffect, useState } from 'react';
import { PanelSectionRow, ToggleField } from '@decky/ui';
import { callable } from '@decky/api';

type Status = 'active' | 'off' | 'restart' | 'unavailable' | 'unknown';

interface ProtonBridgeState {
    enabled: boolean;
    status: Status;
}

const getProtonBridge = callable<[], ProtonBridgeState>('get_proton_bridge');
const setProtonBridge = callable<[boolean], ProtonBridgeState>('set_proton_bridge');

const DESCRIPTIONS: Record<Status, string> = {
    active: 'Windows games under Proton can send their own Discord status.',
    off: 'Proton games only show the game name.',
    restart: 'Restart Game Mode (or Steam on the desktop) to apply.',
    unavailable: 'bridge.exe is missing from this install of the plugin.',
    unknown: 'Applies to games started after Steam restarts.'
};

/** Runs rpc-bridge inside every Proton game, so a game's own rich presence reaches Discord. */
const ProtonSettings: FC<{}> = () => {
    const [state, setState] = useState<ProtonBridgeState | null>(null);

    useEffect(() => {
        getProtonBridge().then(setState);
    }, []);

    const toggle = useCallback(async (enabled: boolean) => setState(await setProtonBridge(enabled)), []);

    if (!state) {
        return null;
    }

    return (
        <PanelSectionRow>
            <ToggleField
                label="Rich presence from Proton games"
                description={DESCRIPTIONS[state.status]}
                checked={state.enabled}
                disabled={state.status === 'unavailable'}
                onChange={toggle}
            />
        </PanelSectionRow>
    );
};

export default ProtonSettings;
